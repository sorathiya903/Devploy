from fastapi import FastAPI, Request
import subprocess
import os
import requests
import zipfile
import io
import shutil




BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")

from fastapi.responses import FileResponse, JSONResponse


app = FastAPI()

def get_project(host: str) -> str:
    """
    animationonhands.devploy.run.place → animationonhands
    """
    if not host:
        return ""

    parts = host.split(".")
    if len(parts) < 3:
        return ""

    return parts[0]


@app.get("/{full_path:path}")
def serve_static(request: Request, full_path: str):
    host = request.headers.get("host", "")
    project = get_project(host)

    if not project:
        return JSONResponse({"error": "Invalid project"}, status_code=400)

    project_path = os.path.join(BASE_DIR, project)

    # If project doesn't exist
    if not os.path.exists(project_path):
        return JSONResponse({"error": "Project not found"}, status_code=404)

    # Default route → index.html
    if full_path == "" or full_path == "/":
        index_file = os.path.join(project_path, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return JSONResponse({"error": "index.html missing"}, status_code=404)

    # Serve static assets (css/js/images/etc)
    file_path = os.path.join(project_path, full_path)

    # Security: prevent path traversal
    if not file_path.startswith(project_path):
        return JSONResponse({"error": "Invalid path"}, status_code=403)

    if os.path.exists(file_path):
        return FileResponse(file_path)

    # fallback → try index.html (SPA support like React)
    index_file = os.path.join(project_path, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)

    return JSONResponse({"error": "File not found"}, status_code=404)


@app.post("/deploy")
def deploy(project_name: str, repo_url: str):

    base_path = f"/opt/render/project/src/backend/projects/{project_name}"

    subprocess.run(["git", "clone", repo_url, base_path], check=True)

    # Find nested folder (GitHub usually creates one folder inside)
    items = os.listdir(base_path)

    if len(items) == 1 and os.path.isdir(os.path.join(base_path, items[0])):
        nested_path = os.path.join(base_path, items[0])

        # Move everything up
        for item in os.listdir(nested_path):
            shutil.move(
                os.path.join(nested_path, item),
                base_path
            )

        shutil.rmtree(nested_path)

    return {
        "url": f"https://{project_name}.devploy.run.place"
    }

@app.get("/files")
def files():

    return {
        "cwd": os.getcwd(),
        "base_dir": BASE_DIR,
        "projects_dir": PROJECTS_DIR,
        "base_dir_files": os.listdir(BASE_DIR),
        "projects": os.listdir(PROJECTS_DIR) if os.path.exists(PROJECTS_DIR) else []
    }

@app.get("/tree")
def tree():

    data = {}

    for root, dirs, files in os.walk(BASE_DIR):
        data[root] = files

    return data



@app.get("/")
def welcome():
    return {"message":"Server running"}
