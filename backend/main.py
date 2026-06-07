from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from pymongo import MongoClient

from jose import jwt, JWTError

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from datetime import datetime, timedelta
from pydantic import BaseModel
import tempfile
import subprocess
import shutil
import traceback
import os


# ==========================================
# CONFIG
# ==========================================


class DeployRequest(BaseModel):
    project_name: str
    repo_url: str
    base_dir: str = ""

BASE_DIR = "/opt/render/project/src/backend"
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")

os.makedirs(PROJECTS_DIR, exist_ok=True)

MONGO_URI = os.getenv("MONGO_URI")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret")

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

@app.post("/deploy")
def deploy(data: DeployRequest, authorization: str = Header(None)):

    username = current_user(authorization)

    project_name = data.project_name.lower().strip()
    repo_url = data.repo_url.strip()
    base_dir = data.base_dir.strip()

    project_path = os.path.join(PROJECTS_DIR, project_name)
    temp_clone = os.path.join("/tmp", f"clone_{project_name}")

    # clean old temp
    if os.path.exists(temp_clone):
        shutil.rmtree(temp_clone)

    # create DB entry FIRST (important for logs)
    projects.insert_one({
        "owner": username,
        "name": project_name,
        "url": "",
        "repo_url": repo_url,
        "base_dir": base_dir,
        "status": "queued",
        "logs": [],
        "created_at": datetime.utcnow()
    })

    def log(msg, status=None):
        update = {"$push": {"logs": msg}}
        if status:
            update["$set"] = {"status": status}

        projects.update_one(
            {"owner": username, "name": project_name},
            update
        )

    try:

        log("🚀 Deployment started", "cloning")

        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, temp_clone],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            log("❌ Git clone failed", "failed")
            return {"success": False, "error": result.stderr}

        log("📦 Repository cloned", "building")

        source_folder = temp_clone

        if base_dir:
            source_folder = os.path.join(temp_clone, base_dir)

            if not os.path.isdir(source_folder):
                log("❌ Base directory not found", "failed")
                return {"success": False, "error": "Base dir not found"}

        index_file = os.path.join(source_folder, "index.html")

        if not os.path.exists(index_file):
            log("❌ index.html missing", "failed")
            return {"success": False, "error": "index.html missing"}

        log("📁 Preparing files...", "finalizing")

        if os.path.exists(project_path):
            shutil.rmtree(project_path)

        shutil.copytree(source_folder, project_path)

        url = f"https://{project_name}.devploy.run.place"

        projects.update_one(
            {"owner": username, "name": project_name},
            {"$set": {
                "status": "deployed",
                "url": url
            }}
        )

        log("🎉 Published successfully", "deployed")

        return {
            "success": True,
            "url": url
        }

    except Exception as e:

        log(f"🔥 Error: {str(e)}", "failed")

        return {
            "success": False,
            "error": str(e)
        }

    finally:
        if os.path.exists(temp_clone):
            shutil.rmtree(temp_clone)
            
            
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
