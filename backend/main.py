from fastapi import FastAPI
import subprocess
import os
import requests
import zipfile
import io


app = FastAPI()

@app.post("/deploy")
def deploy(project_name: str, repo_url: str):

    os.makedirs("/app/projects", exist_ok=True)

    # Convert:
    # https://github.com/user/repo.git
    # ->
    # https://github.com/user/repo/archive/refs/heads/main.zip

    repo_url = repo_url.removesuffix(".git")
    zip_url = f"{repo_url}/archive/refs/heads/main.zip"

    project_path = f"/app/projects/{project_name}"

    response = requests.get(zip_url)

    if response.status_code != 200:
        return {"error": "Could not download repository"}

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        z.extractall(project_path)

    return {
        "success": True,
        "url": f"https://{project_name}.devploy.run.place"
    }


@app.get("/")
def welcome():
    return {"message":"Server running"}
