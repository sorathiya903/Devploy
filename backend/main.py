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

# ==========================================
# CONFIG
# ==========================================


class DeployRequest(BaseModel):
    project_name: str
    repo_url: str
    base_dir: str = ""

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
    sha
):

    async def run():

        try:

            repo_path = f"/tmp/{project_name}"

            if os.path.exists(repo_path):
                shutil.rmtree(repo_path)

            await push_log(
                project_name,
                f"Deploying commit {sha[:7]}",
                "cloning"
            )

            result = subprocess.run(
                [
                    "git",
                    "clone",
                    repo_url,
                    repo_path
                ],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                await push_log(
                    project_name,
                    result.stderr,
                    "failed"
                )
                return

            checkout = subprocess.run(
                [
                    "git",
                    "-C",
                    repo_path,
                    "checkout",
                    sha
                ],
                capture_output=True,
                text=True
            )

            if checkout.returncode != 0:
                await push_log(
                    project_name,
                    checkout.stderr,
                    "failed"
                )
                return

            source_path = repo_path

            if base_dir:
                source_path = os.path.join(
                    repo_path,
                    base_dir
                )

            final_path = os.path.join(
                PROJECTS_DIR,
                project_name
            )

            if os.path.exists(final_path):
                shutil.rmtree(final_path)

            os.makedirs(final_path)

            for item in os.listdir(source_path):

                if item == ".git":
                    continue

                src = os.path.join(
                    source_path,
                    item
                )

                dst = os.path.join(
                    final_path,
                    item
                )

                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

            projects.update_one(
                {
                    "name": project_name
                },
                {
                    "$set": {
                        "deployed_sha": sha,
                        "status": "deployed"
                    }
                }
            )

            await push_log(
                project_name,
                "Commit deployed successfully 🚀",
                "deployed"
            )

        except Exception as e:

            await push_log(
                project_name,
                str(e),
                "failed"
            )

    asyncio.run(run())

            
def deploy_worker(project_name, repo_url, base_dir, username):


    async def run():
        try:
            repo_path = f"/tmp/{project_name}"

            await push_log(project_name, "Queued deployment", "queued")

            # -------------------------
            # CLONE REPOSITORY
            # -------------------------
            await push_log(project_name, "Cloning repository...", "cloning")

            result = subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, repo_path],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                await push_log(project_name, "Clone failed", "failed")
                await push_log(project_name, result.stderr or "git error", "failed")
                return

            await push_log(project_name, "Repository cloned", "cloned")

            # -------------------------
            # RESOLVE SOURCE PATH
            # -------------------------
            source_path = repo_path

            if base_dir and base_dir.strip():
                source_path = os.path.join(repo_path, base_dir.strip())

            # safety check
            if not os.path.exists(source_path):
                await push_log(
                    project_name,
                    f"Base directory not found: {base_dir}",
                    "failed"
                )
                return

            index_file = os.path.join(source_path, "index.html")

            if not os.path.exists(index_file):
                await push_log(
                    project_name,
                    "index.html not found in selected directory",
                    "failed"
                )
                return

            # -------------------------
            # BUILD STEP (optional placeholder)
            # -------------------------
            await push_log(project_name, "Building project...", "building")
            time.sleep(1)

            # -------------------------
            # COPY ONLY BASE DIR CONTENTS
            # -------------------------
            final_path = os.path.join(PROJECTS_DIR, project_name)

            if os.path.exists(final_path):
                shutil.rmtree(final_path)

            os.makedirs(final_path, exist_ok=True)

            # copy only contents (not .git, not full repo)
            for item in os.listdir(source_path):

                if item == ".git":
                    continue  # skip git metadata

                src_item = os.path.join(source_path, item)
                dst_item = os.path.join(final_path, item)

                if os.path.isdir(src_item):
                    shutil.copytree(src_item, dst_item)
                else:
                    shutil.copy2(src_item, dst_item)

            # -------------------------
            # FINALIZE
            # -------------------------
            await push_log(project_name, "Finalizing deployment...", "finalizing")
            time.sleep(1)

            await push_log(project_name, "Deployment complete 🚀", "deployed")

        except Exception as e:
            await push_log(project_name, f"Error: {str(e)}", "failed")

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
        args=(
            project["name"],
            project["repo_url"],
            project["base_dir"],
            data.sha
        )
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
def deploy(data: DeployRequest, authorization: str = Header(None)):

    username = current_user(authorization)

    project_name = data.project_name.strip().lower().replace(" ", "-")

    # CREATE PROJECT ENTRY IN MONGO
    projects.update_one(
        {"name": project_name},
        {
            "$setOnInsert": {
                "name": project_name,
                "owner": username,
                "repo_url": data.repo_url,
                "base_dir": data.base_dir,
                "status": "queued",
                "deployed_sha": "",
                "url": f"https://{project_name}.devploy.run.place",
                "logs": [],
                "created_at": datetime.utcnow()
            }
        },
        upsert=True
    )

    threading.Thread(
        target=deploy_worker,
        args=(project_name, data.repo_url, data.base_dir, username)
    ).start()

    return {
        "success": True,
        "message": "Deployment started",
        "project": project_name,
        "ws": f"wss://devploy.onrender.com/ws/deploy/{project_name}"
    }


# ==========================================
# DEBUG
# ==========================================

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

    host = request.headers.get(
        "host",
        ""
    )

    project = get_project(host)

    if not project:
        return JSONResponse(
            {
                "error": "Invalid host"
            },
            status_code=400
        )

    project_path = os.path.join(
        PROJECTS_DIR,
        project
    )

    file_path = os.path.join(
        project_path,
        full_path
    )

    if os.path.exists(file_path):
        return FileResponse(file_path)

    index_file = os.path.join(
        project_path,
        "index.html"
    )

    if os.path.exists(index_file):
        return FileResponse(index_file)

    return JSONResponse(
        {
            "error": "Not found"
        },
        status_code=404
    )
