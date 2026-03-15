import os
import secrets
import re
from datetime import datetime, timezone

import uvicorn
import redis as redis_lib
from fastapi import FastAPI, Cookie, Response
from fastapi.responses import JSONResponse
from typing import Optional

app = FastAPI()

SESSION_COOKIE = "X-Session-Id"
SID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def get_redis() -> redis_lib.Redis:
    return redis_lib.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        password=os.environ.get("REDIS_PASSWORD") or None,
        db=int(os.environ.get("REDIS_DB", 0)),
        decode_responses=True,
    )


def get_ttl() -> int:
    return int(os.environ["APP_USER_SESSION_TTL"])


def new_sid() -> str:
    return secrets.token_hex(16)


def is_valid_sid(sid: str) -> bool:
    return bool(SID_PATTERN.match(sid))


def set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        httponly=True,
        path="/",
        max_age=get_ttl(),
    )


@app.get("/health")
async def health(response: Response, x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    if x_session_id and is_valid_sid(x_session_id):
        set_session_cookie(response, x_session_id)
    return JSONResponse({"status": "ok"}, headers=dict(response.headers))


@app.post("/session")
async def session(x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    r = get_redis()
    ttl = get_ttl()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if x_session_id and is_valid_sid(x_session_id):
        key = f"sid:{x_session_id}"
        if r.exists(key):
            r.hset(key, "updated_at", now)
            r.expire(key, ttl)
            response = Response(status_code=200)
            set_session_cookie(response, x_session_id)
            return response

    while True:
        sid = new_sid()
        key = f"sid:{sid}"
        created = r.hsetnx(key, "created_at", now)
        if created:
            r.hset(key, "updated_at", now)
            r.expire(key, ttl)
            break

    response = Response(status_code=201)
    set_session_cookie(response, sid)
    return response


def run():
    host = os.environ["APP_HOST"]
    port = int(os.environ["APP_PORT"])
    uvicorn.run(app, host=host, port=port)
