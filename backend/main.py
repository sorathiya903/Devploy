from fastapi import FastAPI
import subprocess
import os
import requests
import zipfile
import io
import shutil

app = FastAPI()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")


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
