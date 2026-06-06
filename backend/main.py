from fastapi import FastAPI
import subprocess
import os

app = FastAPI()


@app.post("/deploy")
def deploy(project_name: str, repo_url: str):

    os.makedirs("/app/projects", exist_ok=True)

    path = f"/app/projects/{project_name}"

    result = subprocess.run(
        ["git", "clone", repo_url, path],
        capture_output=True,
        text=True
    )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr
    }


@app.get("/")
def welcome():
    return {"message":"Server running"}
