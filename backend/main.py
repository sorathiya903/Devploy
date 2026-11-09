from fastapi import FastAPI, Request, HTTPException, Header, WebSocket 
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, List
from pymongo import MongoClient
import time
from jose import jwt, JWTError
import json
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
import threading
from datetime import datetime, timedelta
from pydantic import BaseModel
import tempfile
import subprocess
import shutil
import traceback
import os
import asyncio
import requests 
import re
from fastapi.responses import RedirectResponse
import boto3
from botocore.config import Config
import mimetypes
import uuid

from botocore.exceptions import ClientError
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
# ==========================================
# CONFIG
# ==========================================
B2_KEY_ID = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME")
B2_ENDPOINT = os.getenv("B2_ENDPOINT")
B2_REGION = os.getenv("B2_REGION")

s3_client = boto3.client(
    "s3",
    endpoint_url=B2_ENDPOINT,
    aws_access_key_id=B2_KEY_ID,
    aws_secret_access_key=B2_APPLICATION_KEY,
    region_name=B2_REGION,
    config=Config(signature_version="s3v4")
)

class DeployRequest(BaseModel):
    project_name: str
    repo_url: str
    base_dir: str = ""
    auto_deploy: bool = False

class CommitDeployRequest(BaseModel):
    project_name: str
    sha: str

BASE_DIR = "/opt/render/project/src/backend"
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")

os.makedirs(PROJECTS_DIR, exist_ok=True)



MONGO_URI = os.getenv("MONGO_URI")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")



ALGORITHM = "HS256"

client = MongoClient(MONGO_URI)

db = client["devploy"]

users = db["users"]
projects = db["projects"]

ph = PasswordHasher()

app = FastAPI()


# ==========================================
# CORS
# ==========================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================
# JWT
# ==========================================

def b2_project_prefix(project_name, deployment_id):
    return f"projects/{project_name}/{deployment_id}/"


def upload_project_to_b2(local_path, project_name, deployment_id):

    prefix = b2_project_prefix(project_name, deployment_id)
    uploaded = 0

    for root, dirs, files in os.walk(local_path, followlinks=False):

        # Do not follow symbolic links
        dirs[:] = [
            d for d in dirs
            if not os.path.islink(os.path.join(root, d))
        ]

        for filename in files:

            file_path = os.path.join(root, filename)

            if os.path.islink(file_path):
                continue

            relative_path = os.path.relpath(
                file_path,
                local_path
            ).replace(os.sep, "/")

            key = prefix + relative_path

            content_type, _ = mimetypes.guess_type(file_path)

            s3_client.upload_file(
                file_path,
                B2_BUCKET_NAME,
                key,
                ExtraArgs={
                    "ContentType": content_type or
                    "application/octet-stream"
                }
            )

            uploaded += 1

    return uploaded


def delete_b2_prefix(prefix):

    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=B2_BUCKET_NAME,
        Prefix=prefix
    ):

        objects = page.get("Contents", [])

        for i in range(0, len(objects), 1000):

            batch = objects[i:i + 1000]

            if batch:
                s3_client.delete_objects(
                    Bucket=B2_BUCKET_NAME,
                    Delete={
                        "Objects": [
                            {"Key": obj["Key"]}
                            for obj in batch
                        ],
                        "Quiet": True
                    }
                )


def get_b2_object(key):

    try:
        return s3_client.get_object(
            Bucket=B2_BUCKET_NAME,
            Key=key
        )

    except ClientError as e:

        code = e.response.get(
            "Error", {}
        ).get("Code")

        if code in ("NoSuchKey", "404", "NotFound"):
            return None

        raise


def serve_b2_object(obj):

    body = obj["Body"]

    headers = {
        "Cache-Control": "public, max-age=300"
    }

    if obj.get("ContentLength") is not None:
        headers["Content-Length"] = str(obj["ContentLength"])

    return StreamingResponse(
        body.iter_chunks(chunk_size=1024 * 1024),
        media_type=obj.get(
            "ContentType",
            "application/octet-stream"
        ),
        headers=headers,
        background=BackgroundTask(body.close)
    )

def github_repo_parts(url):

    url = url.strip()

    url = url.replace(
        "https://github.com/",
        ""
    )

    url = url.rstrip("/")

    if url.endswith(".git"):
        url = url[:-4]

    parts = url.split("/")

    if len(parts) < 2:
        raise Exception("Invalid GitHub URL")

    owner = parts[0]
    repo = parts[1]

    return owner, repo

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, project: str, websocket: WebSocket):
        await websocket.accept()

        if project not in self.active_connections:
            self.active_connections[project] = []

        self.active_connections[project].append(websocket)

    def disconnect(self, project: str, websocket: WebSocket):
        if project in self.active_connections:
            if websocket in self.active_connections[project]:
                self.active_connections[project].remove(websocket)

    async def send(self, project: str, message: dict):
        if project not in self.active_connections:
            return

        dead = []

        for conn in self.active_connections[project]:
            try:
                await conn.send_text(json.dumps(message))
            except Exception:
                dead.append(conn)

        # remove dead sockets
        for d in dead:
            self.disconnect(project, d)


manager = ConnectionManager()


async def push_log(project, message, status=None):
    data = {
        "msg": message,
        "status": status
    }

    # DB update
    projects.update_one(
        {"name": project},
        {
            "$push": {"logs": data},
            "$set": {"status": status} if status else {}
        }
    )

    # safe websocket broadcast
    await manager.send(project, data)


@app.get("/b2-test")
def b2_test():
    try:
        response = s3_client.list_objects_v2(
            Bucket=B2_BUCKET_NAME,
            MaxKeys=1
        )

        return {
            "success": True,
            "message": "Backblaze B2 connected!",
            "bucket": B2_BUCKET_NAME,
            "connection": "OK"
        }

    except Exception as e:
        print("B2 TEST ERROR:", repr(e))

        raise HTTPException(
            status_code=500,
            detail=f"B2 connection failed: {str(e)}"
        )
@app.websocket("/ws/deploy/{project_name}")
async def deploy_ws(websocket: WebSocket, project_name: str):

    await manager.connect(project_name, websocket)

    try:
        while True:
            await asyncio.sleep(30)
    except:
        manager.disconnect(project_name, websocket)



@app.get("/project/{project_name}")
def get_project_info(
    project_name: str,
    authorization: str = Header(None)
):

    username = current_user(authorization)

    project = projects.find_one(
        {
            "name": project_name,
            "owner": username
        },
        {
            "_id": 0
        }
    )

    if not project:
        raise HTTPException(404, "Project not found")

    return project


@app.get("/project-status/{project_name}")
def project_status(
    project_name: str,
    authorization: str = Header(None)
):

    username = current_user(authorization)

    project = projects.find_one(
        {
            "name": project_name,
            "owner": username
        },
        {
            "_id": 0,
            "status": 1,
            "deployed_sha": 1,
            "url": 1
        }
    )

    if not project:
        raise HTTPException(404)

    return project



@app.get("/project-logs/{project_name}")
def project_logs(
    project_name: str,
    authorization: str = Header(None)
):

    username = current_user(authorization)

    project = projects.find_one(
        {
            "name": project_name,
            "owner": username
        }
    )

    if not project:
        raise HTTPException(404)

    return project.get("logs", [])




@app.get("/project-commits/{project_name}")
def project_commits(
    project_name: str,
    authorization: str = Header(None)
):

    username = current_user(authorization)

    project = projects.find_one({
        "name": project_name,
        "owner": username
    })

    if not project:
        raise HTTPException(
            status_code=404,
            detail="Project not found"
        )

    owner, repo = github_repo_parts(
        project["repo_url"]
    )

    github_token = os.getenv("GITHUB_TOKEN")

    headers = {
        "Accept": "application/vnd.github+json"
    }

    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:

        res = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits?per_page=20",
            headers=headers,
            timeout=15
        )

        data = res.json()

        if not isinstance(data, list):
            return {
                "github_error": data
            }

        commits = []

        for c in data:

            commits.append({
                "sha": c.get("sha", ""),
                "message": c["commit"]["message"],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"]
            })

        return commits

    except Exception as e:

        return {
            "error": str(e)
    }

def deploy_commit_worker(
    project_name,
    repo_url,
    base_dir,
    sha,
    username
):

    async def run():

        deployment_id = uuid.uuid4().hex

        new_prefix = b2_project_prefix(
            project_name,
            deployment_id
        )

        activated = False

        repo_parent = tempfile.mkdtemp(
            prefix="devploy-"
        )

        repo_path = os.path.join(
            repo_parent,
            "repo"
        )

        try:

            existing = projects.find_one({
                "name": project_name,
                "owner": username
            })

            if not existing:
                raise Exception("Project not found")

            old_deployment_id = existing.get(
                "active_deployment_id"
            )

            await push_log(
                project_name,
                f"Deploying commit {sha[:7]}",
                "cloning"
            )

            result = subprocess.run(
                ["git", "clone", repo_url, repo_path],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                raise Exception(result.stderr)

            checkout = subprocess.run(
                [
                    "git",
                    "-C",
                    repo_path,
                    "checkout",
                    sha
                ],
                capture_output=True,
                text=True,
                timeout=120
            )

            if checkout.returncode != 0:
                raise Exception(checkout.stderr)

            source_path = repo_path

            if base_dir and base_dir.strip():

                source_path = os.path.realpath(
                    os.path.join(repo_path, base_dir.strip())
                )

                if os.path.commonpath(
                    [repo_path, source_path]
                ) != repo_path:
                    raise Exception("Invalid base directory")

            if not os.path.isfile(
                os.path.join(source_path, "index.html")
            ):
                raise Exception("index.html not found")

            await push_log(
                project_name,
                "Uploading commit files to B2...",
                "uploading"
            )

            uploaded = upload_project_to_b2(
                source_path,
                project_name,
                deployment_id
            )

            if uploaded == 0:
                raise Exception("No project files uploaded")

            result = projects.update_one(
                {
                    "name": project_name,
                    "owner": username
                },
                {
                    "$set": {
                        "active_deployment_id": deployment_id,
                        "deployed_sha": sha,
                        "status": "deployed"
                    }
                }
            )

            if result.matched_count == 0:
                raise Exception("Project record not found")

            activated = True

            if old_deployment_id:

                try:
                    delete_b2_prefix(
                        b2_project_prefix(
                            project_name,
                            old_deployment_id
                        )
                    )
                except Exception as cleanup_error:
                    print("Old version cleanup failed:", cleanup_error)

            await push_log(
                project_name,
                "Commit deployed successfully 🚀",
                "deployed"
            )

        except Exception as e:

            if not activated:

                try:
                    delete_b2_prefix(new_prefix)
                except Exception as cleanup_error:
                    print("B2 cleanup failed:", cleanup_error)

            await push_log(
                project_name,
                f"Commit deployment failed: {str(e)}",
                "failed"
            )

        finally:

            shutil.rmtree(
                repo_parent,
                ignore_errors=True
            )

    asyncio.run(run())

def deploy_worker(project_name, repo_url, base_dir, username):

    async def run():

        deployment_id = uuid.uuid4().hex
        new_prefix = b2_project_prefix(
            project_name,
            deployment_id
        )

        activated = False
        repo_parent = tempfile.mkdtemp(
            prefix="devploy-"
        )

        repo_path = os.path.join(
            repo_parent,
            "repo"
        )

        try:

            existing = projects.find_one({
                "name": project_name,
                "owner": username
            })

            if not existing:
                raise Exception("Project not found")

            old_deployment_id = existing.get(
                "active_deployment_id"
            )

            await push_log(
                project_name,
                "Queued deployment",
                "queued"
            )

            await push_log(
                project_name,
                "Cloning repository...",
                "cloning"
            )

            result = subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    repo_url,
                    repo_path
                ],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                raise Exception(
                    result.stderr or "Git clone failed"
                )

            source_path = repo_path

            if base_dir and base_dir.strip():

                source_path = os.path.realpath(
                    os.path.join(
                        repo_path,
                        base_dir.strip()
                    )
                )

                if os.path.commonpath(
                    [repo_path, source_path]
                ) != repo_path:
                    raise Exception("Invalid base directory")

            if not os.path.isdir(source_path):
                raise Exception("Base directory not found")

            if not os.path.isfile(
                os.path.join(source_path, "index.html")
            ):
                raise Exception("index.html not found")

            await push_log(
                project_name,
                "Uploading project files to Backblaze...",
                "uploading"
            )

            uploaded = upload_project_to_b2(
                source_path,
                project_name,
                deployment_id
            )

            if uploaded == 0:
                raise Exception("No project files uploaded")

            await push_log(
                project_name,
                f"Uploaded {uploaded} files",
                "finalizing"
            )

            result = projects.update_one(
                {
                    "name": project_name,
                    "owner": username
                },
                {
                    "$set": {
                        "active_deployment_id": deployment_id,
                        "deployed_sha": subprocess.run(
                            [
                                "git",
                                "-C",
                                repo_path,
                                "rev-parse",
                                "HEAD"
                            ],
                            capture_output=True,
                            text=True
                        ).stdout.strip(),
                        "status": "deployed"
                    }
                }
            )

            if result.matched_count == 0:
                raise Exception("Project record not found")

            activated = True

            # Remove old version only after new version is active
            if old_deployment_id:

                try:
                    delete_b2_prefix(
                        b2_project_prefix(
                            project_name,
                            old_deployment_id
                        )
                    )

                except Exception as cleanup_error:
                    print(
                        "Old deployment cleanup failed:",
                        cleanup_error
                    )

            await push_log(
                project_name,
                "Deployment complete 🚀",
                "deployed"
            )

        except Exception as e:

            # If upload failed, remove incomplete new version
            if not activated:

                try:
                    delete_b2_prefix(new_prefix)
                except Exception as cleanup_error:
                    print("B2 cleanup failed:", cleanup_error)

            await push_log(
                project_name,
                f"Deployment failed: {str(e)}",
                "failed"
            )

        finally:

            shutil.rmtree(
                repo_parent,
                ignore_errors=True
            )

    asyncio.run(run())

@app.post("/deploy-commit")
def deploy_commit(
    data: CommitDeployRequest,
    authorization: str = Header(None)
):

    username = current_user(authorization)

    project = projects.find_one({
        "name": data.project_name,
        "owner": username
    })

    if not project:
        raise HTTPException(404, "Project not found")

    threading.Thread(
        target=deploy_commit_worker,
        args=(    project["name"],   project["repo_url"],   project["base_dir"],   data.sha,    username)
    ).start()

    return {
        "success": True
    }



def create_token(username):
    payload = {
        "username": username,
        "exp": datetime.utcnow() + timedelta(days=30)
    }

    return jwt.encode(
        payload,
        JWT_SECRET,
        algorithm=ALGORITHM
    )


def verify_token(token):

    try:
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[ALGORITHM]
        )

    except JWTError:
        return None


def current_user(authorization):

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing token"
        )

    token = authorization.replace(
        "Bearer ",
        ""
    )

    payload = verify_token(token)

    if not payload:
        raise HTTPException(
            status_code=401,
            detail="Invalid token"
        )

    return payload["username"]


# ==========================================
# AUTH
# ==========================================
@app.get("/my-projects")
def my_projects(
    authorization: str = Header(None)
):

    username = current_user(
        authorization
    )

    data = list(
        projects.find(
            {
                "owner": username
            },
            {
                "_id": 0
            }
        )
    )

    return data




@app.post("/register")
def register(
    username: str,
    password: str
):

    username = username.lower().strip()

    if users.find_one({
        "username": username
    }):
        raise HTTPException(
            status_code=400,
            detail="Username already exists"
        )

    users.insert_one({
        "username": username,
        "password": ph.hash(password),
        "created_at": datetime.utcnow()
    })

    return {
        "success": True
    }


@app.post("/login")
def login(
    username: str,
    password: str
):

    username = username.lower().strip()

    user = users.find_one({
        "username": username
    })

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )

    try:
        ph.verify(
            user["password"],
            password
        )

    except VerifyMismatchError:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )

    token = create_token(username)

    return {
        "success": True,
        "token": token
    }


@app.get("/me")
def me(
    authorization: str = Header(None)
):

    username = current_user(
        authorization
    )

    return {
        "username": username
    }


# ==========================================
# DEPLOY
# ==========================================



@app.get("/debug/files")
def debug_files():

    def scan_dir(path):
        result = {}

        try:
            for item in os.listdir(path):
                full_path = os.path.join(path, item)

                if os.path.isdir(full_path):
                    result[item] = scan_dir(full_path)
                else:
                    result[item] = "file"

        except Exception as e:
            return {"error": str(e)}

        return result


    return {
        "projects_dir": PROJECTS_DIR,
        "structure": scan_dir(PROJECTS_DIR)
    }

@app.post("/deploy")
def deploy(
    data: DeployRequest,
    authorization: str = Header(None)
):

    username = current_user(authorization)

    project_name = (
        data.project_name
        .strip()
        .lower()
        .replace(" ", "-")
    )

    if not re.fullmatch(r"[a-z0-9-]{1,50}", project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name"  )

    projects.update_one(
        {"name": project_name},
        {
            "$set": {
                "name": project_name,
                "owner": username,
                "repo_url": data.repo_url,
                "base_dir": data.base_dir,
                "auto_deploy": data.auto_deploy,
                "status": "queued",
                 "url": f"https://devploy.netlify.app/{project_name}/"
            },
            "$setOnInsert": {
                "logs": [],
                "deployed_sha": "",
                "created_at": datetime.utcnow()
            }
        },
        upsert=True
    )

    # --------------------------
    # AUTO CREATE GITHUB WEBHOOK
    # --------------------------

    if data.auto_deploy:

        user = users.find_one(
            {"username": username}
        )

        github_token = user.get(
            "github_token"
        )

        if github_token:

            owner, repo = github_repo_parts(
                data.repo_url
            )

            headers = {
                "Authorization":
                f"Bearer {github_token}",
                "Accept":
                "application/vnd.github+json"
            }

            try:

                hooks_res = requests.get(
                    f"https://api.github.com/repos/{owner}/{repo}/hooks",
                    headers=headers
                )

                hooks = hooks_res.json()

                exists = False

                for hook in hooks:

                    config = hook.get(
                        "config",
                        {}
                    )

                    if config.get("url") == \
                    "https://devploy.onrender.com/github/webhook":

                        exists = True
                        break

                if not exists:

                    requests.post(
                        f"https://api.github.com/repos/{owner}/{repo}/hooks",
                        headers=headers,
                        json={
                            "name": "web",
                            "active": True,
                            "events": ["push"],
                            "config": {
                                "url":
                                "https://devploy.onrender.com/github/webhook",
                                "content_type":
                                "json"
                            }
                        }
                    )

            except Exception as e:
                print(
                    "Webhook creation failed:",
                    e
                )

    threading.Thread(
        target=deploy_worker,
        args=(
            project_name,
            data.repo_url,
            data.base_dir,
            username
        )
    ).start()

    return {
        "success": True,
        "project": project_name
                    }


# ==========================================
# DEBUG
# ==========================================



@app.post("/github/webhook")
async def github_webhook(
    request: Request
):

    payload = await request.json()

    repo_url = payload.get(
        "repository",
        {}
    ).get(
        "clone_url"
    )

    sha = payload.get("after")

    if not repo_url or not sha:
        return {"ok": True}

    project = projects.find_one(
        {
            "repo_url": repo_url,
            "auto_deploy": True
        }
    )

    if not project:
        return {"ok": True}

    threading.Thread(
        target=deploy_commit_worker,
        args=(
    project["name"],
   project["repo_url"],    project.get("base_dir", ""),  sha, project["owner"])
    ).start()

    return {
        "success": True
    }



@app.get("/files")
def files():

    return {
        "projects": os.listdir(PROJECTS_DIR)
    }


# ==========================================
# HOST -> PROJECT
# ==========================================

def get_project(host):

    if not host:
        return ""

    host = host.split(":")[0]

    if host.startswith("www."):
        host = host[4:]

    if host.endswith("onrender.com"):
        return ""

    parts = host.split(".")

    if len(parts) < 3:
        return ""

    return parts[0]


# ==========================================
# ROOT
# ==========================================

@app.get("/")
def root(request: Request):

    host = request.headers.get(
        "host",
        ""
    )

    project = get_project(host)

    if not project:
        return {
            "message": "Devploy running 🚀"
        }

    index_file = os.path.join(
        PROJECTS_DIR,
        project,
        "index.html"
    )

    if os.path.exists(index_file):
        return FileResponse(index_file)

    return JSONResponse(
        {
            "error": "Project not found"
        },
        status_code=404
    )


# ==========================================
# STATIC FILES
# ==========================================

@app.get("/github/status")
def github_status(
    authorization: str = Header(None)
):

    username = current_user(authorization)

    user = users.find_one(
        {"username": username}
    )

    return {
        "connected":
        bool(user.get("github_token"))
    }
    


@app.get("/github/repos")
def github_repos(
    authorization: str = Header(None)
):

    username = current_user(authorization)

    user = users.find_one(
        {"username": username}
    )

    github_token = user.get(
        "github_token"
    )

    if not github_token:
        raise HTTPException(
            400,
            "GitHub not connected"
        )

    res = requests.get(
        "https://api.github.com/user/repos?per_page=100",
        headers={
            "Authorization":
            f"Bearer {github_token}",
            "Accept":
            "application/vnd.github+json"
        }
    )

    return res.json()



@app.get("/github/callback")
def github_callback(
    code: str,
    state: str
):

    payload = verify_token(state)

    if not payload:
        raise HTTPException(401)

    username = payload["username"]

    token_res = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={
            "Accept": "application/json"
        },
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code
        }
    )

    access_token = token_res.json()["access_token"]

    users.update_one(
        {"username": username},
        {
            "$set": {
                "github_token": access_token
            }
        }
    )

    return HTMLResponse("""
    <script>
    window.close();
    </script>
    GitHub connected successfully.
    """)

@app.get("/{full_path:path}")
def static_files(
    request: Request,
    full_path: str
):

    parts = full_path.split("/")

    project_name = parts[0].strip()

    if not re.fullmatch(r"[a-z0-9-]{1,50}", project_name):
        raise HTTPException(404, "Project not found")

    project = projects.find_one(
        {"name": project_name},
        {
            "_id": 0,
            "active_deployment_id": 1
        }
    )

    if not project:
        raise HTTPException(404, "Project not found")

    deployment_id = project.get(
        "active_deployment_id"
    )

    if not deployment_id:
        raise HTTPException(404, "Project not deployed")

    if len(parts) == 1 and not full_path.endswith("/"):
        return RedirectResponse(
            url=f"/{project_name}/",
            status_code=307
        )

    relative_path = "/".join(parts[1:])

    if not relative_path:
        relative_path = "index.html"

    # Reject unsafe paths
    if (
        "\\" in relative_path
        or any(p in (".", "..") for p in relative_path.split("/"))
    ):
        raise HTTPException(403, "Invalid path")

    prefix = b2_project_prefix(
        project_name,
        deployment_id
    )

    key = prefix + relative_path

    try:

        obj = get_b2_object(key)

        # SPA fallback for extensionless routes
        if (
            obj is None
            and not os.path.splitext(relative_path)[1]
        ):
            obj = get_b2_object(
                prefix + "index.html"
            )

        if obj is None:
            raise HTTPException(404, "File not found")

        return serve_b2_object(obj)

    except HTTPException:
        raise

    except Exception as e:

        print("B2 FILE SERVING ERROR:", repr(e))

        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve project file"
        )

