from fastapi import APIRouter, HTTPException,Header
from pymongo import MongoClient
from jose import jwt, JWTError 
from datetime import datetime, timedelta
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
import os


router = APIRouter()

# MongoDB
client = MongoClient(os.getenv("MONGO_URI"))
db = client["devploy"]
users = db["users"]

# Argon2
ph = PasswordHasher()

# JWT
SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"


def create_token(username):
    payload = {
        "username": username,
        "exp": datetime.utcnow() + timedelta(days=30)
    }

    return jwt.encode(
        payload,
        SECRET,
        algorithm=ALGORITHM
    )


def verify_token(token: str):
    try:
        payload = jwt.decode(
            token,
            SECRET,
            algorithms=[ALGORITHM]
        )

        return payload

    except JWTError:
        return None


@router.post("/register")
def register(
    username: str,
    password: str
):
    username = username.strip().lower()

    if len(username) < 3:
        raise HTTPException(
            status_code=400,
            detail="Username too short"
        )

    if len(password) < 6:
        raise HTTPException(
            status_code=400,
            detail="Password too short"
        )

    existing = users.find_one({
        "username": username
    })

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Username already exists"
        )

    hashed_password = ph.hash(password)

    users.insert_one({
        "username": username,
        "password": hashed_password,
        "projects": [],
        "created_at": datetime.utcnow()
    })

    token = create_token(username)

    return {
        "success": True,
        "token": token,
        "username": username
    }


@router.post("/login")
def login(
    username: str,
    password: str
):
    username = username.strip().lower()

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
        "token": token,
        "username": username
    }



@router.get("/me")
def me(
    authorization: str = Header(None)
):
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

    user = users.find_one(
        {"username": payload["username"]},
        {"_id": 0, "password": 0}
    )

    return user
