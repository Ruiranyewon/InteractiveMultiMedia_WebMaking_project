"""가입·로그인·세션·계정 삭제 (PRD_step3 F3-1, F3-7)."""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, validator

from . import db, llm

router = APIRouter()
hasher = PasswordHasher()
DUMMY_HASH = hasher.hash("dummy-password-for-timing")  # 없는 계정도 같은 시간이 걸리도록
COOKIE = "session"
SESSION_DAYS = 14
LOGIN_LIMIT = 5  # IP당, 이메일당 분당 시도 횟수
EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[^@\s<>\"']+\.[^@\s<>\"']{2,}$")
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f<>]")


def now_iso(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def clean_text(s: str, limit: int) -> str:
    return CTRL.sub(" ", s).strip()[:limit]


class SignupIn(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=8, max_length=128)
    nickname: Optional[str] = Field(None, max_length=30)

    @validator("email")
    def _email(cls, v):
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("email")
        return v


class LoginIn(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=128)


class NicknameIn(BaseModel):
    nickname: str = Field(..., min_length=1, max_length=30)


def set_session_cookie(request: Request, response: Response, token: str):
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or os.environ.get("COOKIE_SECURE") == "1", path="/")


def new_session(c, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    c.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))  # 만료 세션 정리
    c.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
              (hashlib.sha256(token.encode()).hexdigest(), user_id,
               now_iso(datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS))))
    return token


def user_from_request(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT u.id, u.email, u.nickname FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (hashlib.sha256(token.encode()).hexdigest(), now_iso())).fetchone()
    return dict(row) if row else None


def current_user(request: Request):
    user = user_from_request(request)
    if not user:
        raise HTTPException(401, "로그인이 필요해요.")
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/api/auth/signup", status_code=201)
def signup(body: SignupIn, request: Request, response: Response):
    llm.check_rate_limit(client_ip(request), "signup", 5)
    name = clean_text(body.nickname or "", 30) or body.email.split("@")[0][:30]
    with db.conn() as c:
        try:
            cur = c.execute("INSERT INTO users (email, password_hash, nickname) VALUES (?, ?, ?)",
                            (body.email, hasher.hash(body.password), name))
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "이미 가입된 이메일이에요.")
            raise
        uid = cur.lastrowid
        token = new_session(c, uid)
    set_session_cookie(request, response, token)
    return {"user": {"id": uid, "email": body.email, "nickname": name}}


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    email = body.email.strip().lower()
    llm.check_rate_limit(client_ip(request), "login", LOGIN_LIMIT)
    llm.check_rate_limit(email, "login-email", LOGIN_LIMIT)
    with db.conn() as c:
        row = c.execute("SELECT id, email, nickname, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        ok = False
        try:
            ok = hasher.verify(row["password_hash"] if row else DUMMY_HASH, body.password) and row is not None
        except (VerificationError, InvalidHashError):
            ok = False
        if not ok:
            raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않아요.")
        token = new_session(c, row["id"])
    set_session_cookie(request, response, token)
    return {"user": {"id": row["id"], "email": row["email"], "nickname": row["nickname"]}}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with db.conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
def me(request: Request):
    """로그인하지 않았으면 user=null (401을 내지 않아 화면이 조용히 비로그인 상태로 시작한다)."""
    return {"user": user_from_request(request)}


@router.put("/api/me")
def update_me(body: NicknameIn, user=Depends(current_user)):
    name = clean_text(body.nickname, 30) or user["nickname"]
    with db.conn() as c:
        c.execute("UPDATE users SET nickname = ? WHERE id = ?", (name, user["id"]))
    return {"user": {**user, "nickname": name}}


@router.delete("/api/me")
def delete_account(response: Response, user=Depends(current_user)):
    with db.conn() as c:  # sessions, assessments, saved_places는 ON DELETE CASCADE로 함께 삭제
        c.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
