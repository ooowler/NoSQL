import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import bcrypt
import redis as redis_lib
import uvicorn
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Cookie, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

SESSION_COOKIE = "X-Session-Id"
SID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

MONGO_CLIENT: Optional[MongoClient] = None
MONGO_DATABASE: Optional[Database] = None


def get_ttl() -> int:
    return int(os.environ["APP_USER_SESSION_TTL"])


def get_redis() -> redis_lib.Redis:
    return redis_lib.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        password=os.environ.get("REDIS_PASSWORD") or None,
        db=int(os.environ.get("REDIS_DB", 0)),
        decode_responses=True,
    )


def get_mongo_database() -> Database:
    global MONGO_CLIENT, MONGO_DATABASE
    if MONGO_DATABASE is not None:
        return MONGO_DATABASE
    user = (os.environ.get("MONGODB_USER") or "").strip()
    pwd = (os.environ.get("MONGODB_PASSWORD") or "").strip()
    host = os.environ["MONGODB_HOST"]
    port = int(os.environ["MONGODB_PORT"])
    name = os.environ.get("MONGODB_DATABSE") or os.environ.get("MONGODB_DATABASE", "eventhub")
    if user:
        uri = f"mongodb://{quote_plus(user)}:{quote_plus(pwd)}@{host}:{port}/{name}?authSource=admin"
    else:
        uri = f"mongodb://{host}:{port}/{name}"
    MONGO_CLIENT = MongoClient(uri)
    MONGO_DATABASE = MONGO_CLIENT[name]
    return MONGO_DATABASE


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_mongo_database()
    db.users.create_index("username", unique=True)
    db.events.create_index("title", unique=True)
    db.events.create_index([("title", 1), ("created_by", 1)])
    db.events.create_index("created_by")
    yield


app = FastAPI(lifespan=lifespan)


def new_sid() -> str:
    return secrets.token_hex(16)


def is_valid_sid(sid: str) -> bool:
    return bool(SID_PATTERN.match(sid))


def redis_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_session_cookie(response: Response, sid: str, max_age: Optional[int] = None) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        httponly=True,
        path="/",
        max_age=max_age if max_age is not None else get_ttl(),
    )


def clear_session_cookie(response: Response) -> None:
    response.set_cookie(key=SESSION_COOKIE, value="", httponly=True, path="/", max_age=0)


def touch_session_post(r: redis_lib.Redis, sid: Optional[str], response: Response) -> None:
    if not sid or not is_valid_sid(sid):
        return
    key = f"sid:{sid}"
    if not r.exists(key):
        return
    pipe = r.pipeline()
    pipe.hset(key, "updated_at", redis_ts())
    pipe.expire(key, get_ttl())
    pipe.execute()
    set_session_cookie(response, sid)


def echo_session_get(response: Response, sid: Optional[str]) -> None:
    if sid and is_valid_sid(sid):
        set_session_cookie(response, sid)


def create_fresh_session(r: redis_lib.Redis, user_id_hex: Optional[str] = None) -> str:
    ttl = get_ttl()
    now = redis_ts()
    sid = new_sid()
    key = f"sid:{sid}"
    if not r.hsetnx(key, "created_at", now):
        sid = new_sid()
        key = f"sid:{sid}"
        if not r.hsetnx(key, "created_at", now):
            raise RuntimeError("session id collision")
    mapping: dict[str, str] = {"updated_at": now}
    if user_id_hex:
        mapping["user_id"] = user_id_hex
    pipe = r.pipeline()
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, ttl)
    pipe.execute()
    return sid


def session_user_id(r: redis_lib.Redis, sid: Optional[str]) -> Optional[str]:
    if not sid or not is_valid_sid(sid):
        return None
    key = f"sid:{sid}"
    if not r.exists(key):
        return None
    uid = r.hget(key, "user_id")
    return uid if uid else None


async def parse_json_body(request: Request) -> tuple[Optional[dict], Optional[str]]:
    try:
        data = await request.json()
    except Exception:
        return None, "body"
    if not isinstance(data, dict):
        return None, "body"
    return data, None


def non_empty_str(v: object) -> bool:
    return isinstance(v, str) and bool(v.strip())


def parse_rfc3339_tz(s: str) -> bool:
    if not isinstance(s, str) or not s.strip():
        return False
    t = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return False
    return dt.tzinfo is not None


def parse_uint_q(raw: Optional[str], default: Optional[int]) -> tuple[Optional[int], Optional[str]]:
    if raw is None:
        return default, None
    try:
        v = int(raw)
    except ValueError:
        return None, "invalid"
    if v < 0:
        return None, "invalid"
    return v, None


@app.get("/health")
async def health(x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    out = JSONResponse({"status": "ok"})
    echo_session_get(out, x_session_id)
    return out


@app.post("/session")
async def session(x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    r = get_redis()
    ttl = get_ttl()
    now = redis_ts()
    if x_session_id and is_valid_sid(x_session_id):
        key = f"sid:{x_session_id}"
        if r.exists(key):
            pipe = r.pipeline()
            pipe.hset(key, "updated_at", now)
            pipe.expire(key, ttl)
            pipe.execute()
            response = Response(status_code=200)
            set_session_cookie(response, x_session_id)
            return response

    sid = new_sid()
    key = f"sid:{sid}"
    if not r.hsetnx(key, "created_at", now):
        sid = new_sid()
        key = f"sid:{sid}"
        if not r.hsetnx(key, "created_at", now):
            raise RuntimeError("session id collision")
    pipe = r.pipeline()
    pipe.hset(key, "updated_at", now)
    pipe.expire(key, ttl)
    pipe.execute()

    response = Response(status_code=201)
    set_session_cookie(response, sid)
    return response


@app.post("/users")
async def users_register(
    request: Request,
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    r = get_redis()
    data, bad = await parse_json_body(request)
    if bad:
        out = Response(
            content=json.dumps({"message": f'invalid "{bad}" field'}),
            media_type="application/json",
            status_code=400,
        )
        touch_session_post(r, x_session_id, out)
        return out
    if not x_session_id or not is_valid_sid(x_session_id) or not r.exists(f"sid:{x_session_id}"):
        return Response(
            content=json.dumps({"message": 'invalid "session" field'}),
            media_type="application/json",
            status_code=400,
        )
    for field in ("full_name", "username", "password"):
        if not non_empty_str(data.get(field)):
            out = Response(
                content=json.dumps({"message": f'invalid "{field}" field'}),
                media_type="application/json",
                status_code=400,
            )
            touch_session_post(r, x_session_id, out)
            return out
    pw_hash = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()
    doc = {"full_name": data["full_name"].strip(), "username": data["username"].strip(), "password_hash": pw_hash}
    try:
        ins = get_mongo_database().users.insert_one(doc)
    except DuplicateKeyError:
        out = Response(
            content=json.dumps({"message": "user already exists"}),
            media_type="application/json",
            status_code=409,
        )
        touch_session_post(r, x_session_id, out)
        return out
    uid = str(ins.inserted_id)
    new_sid = create_fresh_session(r, uid)
    out = Response(status_code=201)
    set_session_cookie(out, new_sid)
    return out


@app.post("/auth/login")
async def auth_login(
    request: Request,
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    r = get_redis()
    data, bad = await parse_json_body(request)
    if bad:
        out = Response(
            content=json.dumps({"message": f'invalid "{bad}" field'}),
            media_type="application/json",
            status_code=400,
        )
        touch_session_post(r, x_session_id, out)
        return out
    for field in ("username", "password"):
        if not non_empty_str(data.get(field)):
            out = Response(
                content=json.dumps({"message": f'invalid "{field}" field'}),
                media_type="application/json",
                status_code=400,
            )
            touch_session_post(r, x_session_id, out)
            return out
    u = get_mongo_database().users.find_one({"username": data["username"].strip()})
    if not u or not bcrypt.checkpw(data["password"].encode(), u["password_hash"].encode()):
        out = Response(
            content=json.dumps({"message": "invalid credentials"}),
            media_type="application/json",
            status_code=401,
        )
        touch_session_post(r, x_session_id, out)
        return out
    uid = str(u["_id"])
    key = f"sid:{x_session_id}" if x_session_id and is_valid_sid(x_session_id) else ""
    out = Response(status_code=204)
    if key and r.exists(key):
        r.hset(key, mapping={"user_id": uid, "updated_at": redis_ts()})
        r.expire(key, get_ttl())
        set_session_cookie(out, x_session_id)
    else:
        sid = create_fresh_session(r, uid)
        set_session_cookie(out, sid)
    return out


@app.post("/auth/logout")
async def auth_logout(x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    r = get_redis()
    if x_session_id and is_valid_sid(x_session_id):
        r.delete(f"sid:{x_session_id}")
    out = Response(status_code=204)
    clear_session_cookie(out)
    return out


@app.post("/events")
async def events_create(
    request: Request,
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    r = get_redis()
    uid = session_user_id(r, x_session_id)
    if not uid:
        out = Response(status_code=401)
        touch_session_post(r, x_session_id, out)
        return out
    data, bad = await parse_json_body(request)
    if bad:
        out = Response(
            content=json.dumps({"message": f'invalid "{bad}" field'}),
            media_type="application/json",
            status_code=400,
        )
        touch_session_post(r, x_session_id, out)
        return out
    for field in ("title", "address", "started_at", "finished_at", "description"):
        if field in ("started_at", "finished_at"):
            if not parse_rfc3339_tz(data.get(field, "")):
                out = Response(
                    content=json.dumps({"message": f'invalid "{field}" field'}),
                    media_type="application/json",
                    status_code=400,
                )
                touch_session_post(r, x_session_id, out)
                return out
        elif not non_empty_str(data.get(field)):
            out = Response(
                content=json.dumps({"message": f'invalid "{field}" field'}),
                media_type="application/json",
                status_code=400,
            )
            touch_session_post(r, x_session_id, out)
            return out
    try:
        ObjectId(uid)
    except InvalidId:
        out = Response(status_code=401)
        touch_session_post(r, x_session_id, out)
        return out
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    doc = {
        "title": data["title"].strip(),
        "description": data["description"].strip(),
        "location": {"address": data["address"].strip()},
        "created_at": created_at,
        "created_by": uid,
        "started_at": data["started_at"].strip(),
        "finished_at": data["finished_at"].strip(),
    }
    try:
        ins = get_mongo_database().events.insert_one(doc)
    except DuplicateKeyError:
        out = Response(
            content=json.dumps({"message": "event already exists"}),
            media_type="application/json",
            status_code=409,
        )
        touch_session_post(r, x_session_id, out)
        return out
    out = Response(
        content=json.dumps({"id": str(ins.inserted_id)}),
        media_type="application/json",
        status_code=201,
    )
    touch_session_post(r, x_session_id, out)
    return out


@app.get("/events")
async def events_list(
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
    title: Optional[str] = Query(None),
    limit: Optional[str] = Query(None),
    offset: Optional[str] = Query(None),
):
    lim, le = parse_uint_q(limit, None)
    if le:
        out = Response(
            content=json.dumps({"message": 'invalid "limit" parameter'}),
            media_type="application/json",
            status_code=400,
        )
        echo_session_get(out, x_session_id)
        return out
    off, oe = parse_uint_q(offset, 0)
    if oe:
        out = Response(
            content=json.dumps({"message": 'invalid "offset" parameter'}),
            media_type="application/json",
            status_code=400,
        )
        echo_session_get(out, x_session_id)
        return out
    flt: dict = {}
    if title:
        flt["title"] = re.compile(re.escape(title), re.IGNORECASE)
    cur = get_mongo_database().events.find(flt).sort("_id", -1).skip(off)
    if lim is not None:
        cur = cur.limit(lim)
    rows = list(cur)
    events = [
        {
            "id": str(d["_id"]),
            "title": d["title"],
            "description": d["description"],
            "location": d["location"],
            "created_at": d["created_at"],
            "created_by": d["created_by"],
            "started_at": d["started_at"],
            "finished_at": d["finished_at"],
        }
        for d in rows
    ]
    out = Response(
        content=json.dumps({"events": events, "count": len(events)}),
        media_type="application/json",
        status_code=200,
    )
    echo_session_get(out, x_session_id)
    return out


def run():
    host = os.environ["APP_HOST"]
    port = int(os.environ["APP_PORT"])
    uvicorn.run(app, host=host, port=port)
