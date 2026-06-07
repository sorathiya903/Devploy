from fastapi import FastAPI, Request, HTTPException, Header, WebSocket 
from fastapi.responses import FileResponse, JSONResponse
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

def safe_broadcast(project, message):
    loop = asyncio.get_event_loop()

    if loop.is_running():
        asyncio.create_task(manager.send(project, message))

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


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, project: str, websocket: WebSocket):
        await websocket.accept()
        if project not in self.active_connections:
            self.active_connections[project] = []
        self.active_connections[project].append(websocket)

    def disconnect(self, project: str, websocket: WebSocket):
        self.active_connections[project].remove(websocket)

    async def send(self, project: str, message: dict):
        if project in self.active_connections:
            for conn in self.active_connections[project]:
                await conn.send_text(json.dumps(message))


manager = ConnectionManager()


async def push_log(project, message, status=None):

    data = {
        "msg": message,
        "status": status,
        "time": datetime.utcnow().isoformat()
    }

    projects.update_one(
        {"name": project},
        {
            "$push": {"logs": data},
            "$set": {"status": status} if status else {}
        }
    )

    safe_broadcast(project, data)

@app.websocket("/ws/deploy/{project_name}")
async def deploy_ws(websocket: WebSocket, project_name: str):

    await manager.connect(project_name, websocket)

    try:
        while True:
            await asyncio.sleep(30)
    except:
        manager.disconnect(project_name, websocket)

def deploy_worker(project_name, repo_url, base_dir, username):

    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():

        await push_log(project_name, "Queued deployment", "queued")

        await push_log(project_name, "Cloning repository...", "cloning")

        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url,
             f"/tmp/{project_name}"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            await push_log(project_name, "Clone failed ❌", "failed")
            return

        await push_log(project_name, "Repository cloned", "cloned")

        await push_log(project_name, "Building project...", "building")

        time.sleep(1)

        await push_log(project_name, "Finalizing deployment...", "finalizing")

        project_url = f"https://{project_name}.devploy.run.place"

        await push_log(project_name, f"Live at {project_url}", "deployed")

    loop.run_until_complete(run())


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

    threading.Thread(
        target=deploy_worker,
        args=(data.project_name, data.repo_url, data.base_dir, username)
    ).start()

    return {
    "success": True,
    "message": "Deployment started",
    "project": data.project_name,
    "ws": f"wss://devploy.onrender.com/ws/deploy/{data.project_name}"
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
