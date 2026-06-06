from fastapi import FastAPI
import subprocess
import os
import requests
import zipfile
import io


app = FastAPI()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")


@app.post("/deploy")
def deploy(project_name: str, repo_url: str):

    os.makedirs(PROJECTS_DIR, exist_ok=True)

    project_path = os.path.join(PROJECTS_DIR, project_name)

    if os.path.exists(project_path):
        shutil.rmtree(project_path)

    os.makedirs(project_path)

    repo_url = repo_url.removesuffix(".git")
    zip_url = f"{repo_url}/archive/refs/heads/main.zip"

    response = requests.get(zip_url)

    if response.status_code != 200:
        return {
            "success": False,
            "error": f"Could not download repository ({response.status_code})"
        }

    with zipfile.ZipFile(io.BytesIO(response.content)) as zip_ref:
        zip_ref.extractall(project_path)

    extracted = os.listdir(project_path)

    return {
        "success": True,
        "project": project_name,
        "project_path": project_path,
        "files": extracted
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
