from fastapi import FastAPI
import subprocess
import os

app = FastAPI()

@app.post("/deploy")
def deploy(project_name: str, repo_url: str):

    path = f"/app/projects/{project_name}"

    subprocess.run(
        ["git", "clone", repo_url, path],
        check=True
    )

    return {
        "url": f"https://{project_name}.deploy.run.place"
    }
