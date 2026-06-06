from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
import os
import subprocess
import shutil

app = FastAPI()

# ----------------------------
# BASE PATH CONFIG
# ----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")


# ----------------------------
# HELPERS
# ----------------------------
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


def safe_path(base: str, path: str) -> str:
    """
    Prevent path traversal attacks
    """
    final_path = os.path.join(base, path)
    if not final_path.startswith(base):
        return ""
    return final_path


# ----------------------------
# STATIC SITE HOSTING
# ----------------------------
@app.get("/{full_path:path}")
def serve_static(request: Request, full_path: str):
    host = request.headers.get("host", "")
    project = get_project(host)

    if not project:
        return JSONResponse({"error": "Invalid project"}, status_code=400)

    project_path = os.path.join(PROJECTS_DIR, project)

    if not os.path.exists(project_path):
        return JSONResponse({"error": "Project not found"}, status_code=404)

    # default → index.html
    if full_path == "" or full_path == "/":
        index_file = os.path.join(project_path, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return JSONResponse({"error": "index.html missing"}, status_code=404)

    # serve static assets
    file_path = safe_path(project_path, full_path)

    if not file_path:
        return JSONResponse({"error": "Invalid path"}, status_code=403)

    if os.path.exists(file_path):
        return FileResponse(file_path)

    # fallback SPA support
    index_file = os.path.join(project_path, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)

    return JSONResponse({"error": "File not found"}, status_code=404)


# ----------------------------
# DEPLOY SYSTEM
# ----------------------------
@app.post("/deploy")
def deploy(project_name: str, repo_url: str):

    base_path = os.path.join(PROJECTS_DIR, project_name)

    # clone repo
    subprocess.run(["git", "clone", repo_url, base_path], check=True)

    # fix nested github folder issue
    items = os.listdir(base_path)

    if len(items) == 1:
        nested = os.path.join(base_path, items[0])
        if os.path.isdir(nested):

            for item in os.listdir(nested):
                shutil.move(
                    os.path.join(nested, item),
                    base_path
                )

            shutil.rmtree(nested)

    return {
        "message": "Deployed successfully",
        "url": f"https://{project_name}.devploy.run.place"
    }


# ----------------------------
# DEBUG: FILE SYSTEM INFO
# ----------------------------
@app.get("/files")
def files():
    return {
        "cwd": os.getcwd(),
        "base_dir": BASE_DIR,
        "projects_dir": PROJECTS_DIR,
        "projects": os.listdir(PROJECTS_DIR) if os.path.exists(PROJECTS_DIR) else []
    }


@app.get("/tree")
def tree():

    if not os.path.exists(PROJECTS_DIR):
        return {"error": "projects folder not found"}

    data = {}

    for root, dirs, files in os.walk(PROJECTS_DIR):
        data[root] = files

    return data


# ----------------------------
# HEALTH CHECK
# ----------------------------
@app.get("/")
def welcome():
    return {
        "message": "Devploy server running 🚀"
    }
