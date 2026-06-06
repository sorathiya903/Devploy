from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
import os
import subprocess
import shutil
import traceback

app = FastAPI()

# -------------------------------------------------
# 1. FIXED RENDER-SAFE PATH (VERY IMPORTANT)
# -------------------------------------------------
BASE_DIR = "/opt/render/project/src/backend"
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")


# -------------------------------------------------
# 2. DEBUG INFO ENDPOINT
# -------------------------------------------------
@app.get("/debug")
def debug():
    return {
        "cwd": os.getcwd(),
        "base_dir": BASE_DIR,
        "projects_dir": PROJECTS_DIR,
        "projects_dir_exists": os.path.exists(PROJECTS_DIR),
        "projects_list": os.listdir(PROJECTS_DIR) if os.path.exists(PROJECTS_DIR) else []
    }


# -------------------------------------------------
# 3. HOST → PROJECT NAME
# -------------------------------------------------
def get_project(host: str) -> str:
    if not host:
        return ""

    host = host.split(":")[0].lower()

    # remove www
    if host.startswith("www."):
        host = host.replace("www.", "")

    parts = host.split(".")

    # main Render domain → no project
    if "onrender.com" in host:
        return ""

    # must have at least: project.domain.tld
    if len(parts) < 3:
        return ""

    project = parts[0]

    # system keywords
    if project in ["devploy", "www", "api"]:
        return ""

    return project

@app.post("/deploy")
def deploy(project_name: str, repo_url: str):

    try:
        base_path = os.path.join(PROJECTS_DIR, project_name)

        print("DEPLOY START")
        print("PROJECT:", project_name)
        print("REPO:", repo_url)
        print("TARGET PATH:", base_path)

        os.makedirs(PROJECTS_DIR, exist_ok=True)

        # clone repo
        result = subprocess.run(
            ["git", "clone", repo_url, base_path],
            capture_output=True,
            text=True
        )

        print("GIT STDOUT:", result.stdout)
        print("GIT STDERR:", result.stderr)

        if result.returncode != 0:
            return {
                "error": "Git clone failed",
                "details": result.stderr
            }

        # fix nested folder issue
        items = os.listdir(base_path)
        print("AFTER CLONE:", items)

        if len(items) == 1:
            nested = os.path.join(base_path, items[0])

            if os.path.isdir(nested):
                print("NESTED FOLDER DETECTED:", nested)

                for item in os.listdir(nested):
                    shutil.move(
                        os.path.join(nested, item),
                        base_path
                    )

                shutil.rmtree(nested)

        return {
            "message": "Deploy success",
            "project": project_name,
            "url": f"https://{project_name}.devploy.run.place"
        }

    except Exception as e:
        return {
            "error": "Deploy crashed",
            "exception": str(e),
            "trace": traceback.format_exc()
        }


# -------------------------------------------------
# 6. FILE SYSTEM DEBUG
# -------------------------------------------------
@app.get("/files")
def files():
    return {
        "cwd": os.getcwd(),
        "base_dir": BASE_DIR,
        "projects_dir": PROJECTS_DIR,
        "exists": os.path.exists(PROJECTS_DIR),
        "projects": os.listdir(PROJECTS_DIR) if os.path.exists(PROJECTS_DIR) else []
    }


# -------------------------------------------------
# 7. TREE DEBUG
# -------------------------------------------------
@app.get("/tree")
def tree():

    if not os.path.exists(PROJECTS_DIR):
        return {"error": "projects folder not found"}

    data = {}

    for root, dirs, files in os.walk(PROJECTS_DIR):
        data[root] = files

    return data


# -------------------------------------------------
# 8. HEALTH CHECK
# -------------------------------------------------
@app.get("/")
def home():
    return {
        "message": "Devploy running 🚀",
        "debug_url": "/debug",
        "files_url": "/files"
        }



# -------------------------------------------------
# 4. STATIC FILE SERVER (CORE)
# -------------------------------------------------
@app.get("/{full_path:path}")
def serve(request: Request, full_path: str):

    host = request.headers.get("host", "")
    project = get_project(host)

    print("HOST:", host)
    print("PROJECT:", project)

    if not project:
        return JSONResponse({
            "error": "Invalid host",
            "host_received": host
        }, status_code=400)

    project_path = os.path.join(PROJECTS_DIR, project)

    print("PROJECT PATH:", project_path)
 #   project_path = os.path.join(PROJECTS_DIR, project)

    if not project or not os.path.exists(project_path):
        return JSONResponse({
            "error": "Project not found",
            "requested_project": project,
            "available_projects": os.listdir(PROJECTS_DIR)
        }, status_code=404)

    if not os.path.exists(project_path):
        return JSONResponse({
            "error": "Project not found",
            "checked_path": project_path,
            "available_projects": os.listdir(PROJECTS_DIR) if os.path.exists(PROJECTS_DIR) else []
        }, status_code=404)

    # default route
    if full_path == "" or full_path == "/":
        index_file = os.path.join(project_path, "index.html")

        print("INDEX FILE:", index_file)

        if os.path.exists(index_file):
            return FileResponse(index_file)

        return JSONResponse({
            "error": "index.html missing",
            "project_path": project_path
        }, status_code=404)

    # serve assets safely
    file_path = os.path.join(project_path, full_path)

    print("FILE PATH:", file_path)

    if os.path.exists(file_path):
        return FileResponse(file_path)

    # fallback SPA
    index_file = os.path.join(project_path, "index.html")

    if os.path.exists(index_file):
        return FileResponse(index_file)

    return JSONResponse({
        "error": "File not found",
        "requested": full_path
    }, status_code=404)


# -------------------------------------------------
# 5. DEPLOY (WITH FULL DEBUG LOGS)
# -------------------------------------------------
