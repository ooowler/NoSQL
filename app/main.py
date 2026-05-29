import json
import hashlib
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional
from urllib.parse import quote_plus

import bcrypt
import redis as redis_lib
import uvicorn
from bson import ObjectId
from bson.errors import InvalidId
from cassandra import ConsistencyLevel
from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import Cluster, NoHostAvailable, Session
from cassandra.query import SimpleStatement
from fastapi import Cookie, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from neo4j import GraphDatabase
from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

SESSION_COOKIE = "X-Session-Id"
SID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
JSON = "application/json"
CATEGORIES = {"meetup", "concert", "exhibition", "party", "other"}
DAY_PATTERN = re.compile(r"^\d{8}$")
CQL_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
LIKE_VALUE = 1
DISLIKE_VALUE = -1


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    user = (os.environ.get("MONGODB_USER") or "").strip()
    pwd = (os.environ.get("MONGODB_PASSWORD") or "").strip()
    host = os.environ["MONGODB_HOST"]
    port = int(os.environ["MONGODB_PORT"])
    name = os.environ.get("MONGODB_DATABASE") or os.environ.get("MONGODB_DATABSE") or "eventhub"
    if not user and not pwd:
        uri = f"mongodb://{host}:{port}/{name}"
    else:
        uri = f"mongodb://{quote_plus(user)}:{quote_plus(pwd)}@{host}:{port}/{name}"
        auth_src = (os.environ.get("MONGODB_AUTH_SOURCE") or "").strip()
        if auth_src:
            uri += f"?authSource={quote_plus(auth_src)}"
    client = MongoClient(uri)
    fastapi_app.state.mongo_db = client[name]
    cassandra_cluster, cassandra_session = connect_cassandra()
    select_cassandra_keyspace(cassandra_session)
    fastapi_app.state.cassandra_cluster = cassandra_cluster
    fastapi_app.state.cassandra_session = cassandra_session
    neo4j_driver = connect_neo4j()
    fastapi_app.state.neo4j_driver = neo4j_driver
    try:
        yield
    finally:
        neo4j_driver.close()
        cassandra_cluster.shutdown()
        client.close()


app = FastAPI(lifespan=lifespan)


def get_mongo_database() -> Database:
    return app.state.mongo_db


def get_cassandra_session() -> Session:
    return app.state.cassandra_session


def get_neo4j_driver():
    return app.state.neo4j_driver


def body(data: dict, status: int) -> Response:
    return Response(content=json.dumps(data), media_type=JSON, status_code=status)


def invalid_field(field: str) -> Response:
    return body({"message": f'invalid "{field}" field'}, 400)


def get_ttl() -> int:
    return int(os.environ["APP_USER_SESSION_TTL"])


def get_like_ttl() -> int:
    return int(os.environ.get("APP_LIKE_TTL", "60"))


def get_event_reviews_ttl() -> int:
    return int(os.environ.get("APP_EVENT_REVIEWS_TTL", "120"))


def get_recommendations_ttl() -> int:
    return int(os.environ.get("APP_RECOMMENDATIONS_TTL", "60"))


def get_redis() -> redis_lib.Redis:
    return redis_lib.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        password=os.environ.get("REDIS_PASSWORD") or None,
        db=int(os.environ.get("REDIS_DB", 0)),
        decode_responses=True,
    )


def cql_name(raw: str) -> str:
    value = raw.strip().strip('"').strip("'")
    if not CQL_NAME_PATTERN.match(value):
        raise RuntimeError(f"invalid Cassandra identifier: {raw}")
    return value


def get_cassandra_consistency() -> int:
    raw = (os.environ.get("CASSANDRA_CONSISTENCY") or "ONE").strip().strip('"').upper()
    return getattr(ConsistencyLevel, raw, ConsistencyLevel.ONE)


def connect_cassandra() -> tuple[Cluster, Session]:
    hosts = [h.strip() for h in os.environ["CASSANDRA_HOSTS"].split(",") if h.strip()]
    if not hosts:
        raise RuntimeError("CASSANDRA_HOSTS is empty")
    username = (os.environ.get("CASSANDRA_USERNAME") or "").strip()
    password = os.environ.get("CASSANDRA_PASSWORD") or ""
    auth_provider = PlainTextAuthProvider(username=username, password=password) if username or password else None
    port = int(os.environ.get("CASSANDRA_PORT", "9042"))
    last_error: Optional[Exception] = None
    for _ in range(60):
        cluster = Cluster(hosts, port=port, auth_provider=auth_provider)
        try:
            return cluster, cluster.connect()
        except NoHostAvailable as exc:
            last_error = exc
            cluster.shutdown()
            time.sleep(2)
    if last_error:
        raise last_error
    raise RuntimeError("cannot connect to Cassandra")


def connect_neo4j():
    auth = (os.environ.get("NEO4J_USERNAME") or "", os.environ.get("NEO4J_PASSWORD") or "")
    driver = GraphDatabase.driver(os.environ["NEO4J_URL"], auth=auth)
    last_error: Optional[Exception] = None
    for _ in range(60):
        try:
            driver.verify_connectivity()
            return driver
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    driver.close()
    if last_error:
        raise last_error
    raise RuntimeError("cannot connect to Neo4j")


def cassandra_execute(session: Session, query: str, params: Optional[tuple] = None):
    statement = SimpleStatement(query, consistency_level=get_cassandra_consistency())
    return session.execute(statement, params or ())


def select_cassandra_keyspace(session: Session) -> None:
    keyspace = cql_name(os.environ.get("CASSANDRA_KEYSPACE") or "testkeyspace")
    session.set_keyspace(keyspace)


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


def parse_uint_body(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def parse_day_q(raw: Optional[str]) -> tuple[Optional[datetime], Optional[str]]:
    if raw is None:
        return None, None
    if not DAY_PATTERN.match(raw):
        return None, "invalid"
    try:
        return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc), None
    except ValueError:
        return None, "invalid"


def as_utc_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_dt(value: object) -> object:
    dt = as_utc_datetime(value)
    if not dt:
        return value
    return dt.isoformat().replace("+00:00", "Z")


def doc_ids(doc: dict[str, Any]) -> set[str]:
    ids = set()
    for key in ("_id", "id"):
        if key in doc:
            ids.add(str(doc[key]))
    return ids


def doc_id(doc: dict[str, Any]) -> str:
    if "id" in doc:
        return str(doc["id"])
    if "_id" in doc:
        return str(doc["_id"])
    return ""


def event_title(doc: dict[str, Any]) -> str:
    value = event_value(doc, "title")
    return str(value) if value is not None else ""


def has_id(doc: dict[str, Any], raw_id: str) -> bool:
    return raw_id in doc_ids(doc)


def user_public(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc_id(doc),
        "full_name": doc.get("full_name", ""),
        "username": doc.get("username", ""),
    }


def event_value(doc: dict[str, Any], key: str) -> Any:
    if key in doc:
        return doc[key]
    if key in ("title", "description", "category"):
        return doc.get("content", {}).get(key)
    if key == "price":
        costs = doc.get("costs", {})
        return costs.get("price", costs.get("amount"))
    if key == "created_at":
        return doc.get("created", {}).get("at")
    if key == "created_by":
        return doc.get("created", {}).get("by")
    if key in ("started_at", "finished_at"):
        return doc.get("dates", {}).get(key)
    return None


def event_public(
    doc: dict[str, Any],
    reactions: Optional[dict[str, int]] = None,
    reviews: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": doc_id(doc),
        "title": event_value(doc, "title"),
    }
    category = event_value(doc, "category")
    price = event_value(doc, "price")
    if category is not None:
        out["category"] = category
    if price is not None:
        out["price"] = price
    out.update(
        {
            "description": event_value(doc, "description"),
            "location": doc.get("location", {}),
            "created_at": format_dt(event_value(doc, "created_at")),
            "created_by": str(event_value(doc, "created_by")),
            "started_at": format_dt(event_value(doc, "started_at")),
            "finished_at": format_dt(event_value(doc, "finished_at")),
        }
    )
    if reactions is not None:
        out["reactions"] = reactions
    if reviews is not None:
        out["reviews"] = reviews
    return out


def include_reactions(include: Optional[str]) -> bool:
    if include is None:
        return False
    return "reactions" in {part.strip() for part in include.split(",")}


def include_reviews(include: Optional[str]) -> bool:
    if include is None:
        return False
    return "reviews" in {part.strip() for part in include.split(",")}


def reaction_cache_key(title: str) -> str:
    return f"event:{hashlib.md5(title.encode()).hexdigest()}:reactions"


def empty_reactions() -> dict[str, int]:
    return {"likes": 0, "dislikes": 0}


def parse_reactions(data: dict[str, Any]) -> dict[str, int]:
    return {"likes": int(data.get("likes", 0)), "dislikes": int(data.get("dislikes", 0))}


def cached_reactions(r: redis_lib.Redis, key: str) -> Optional[dict[str, int]]:
    key_type = r.type(key)
    if key_type == "none":
        return None
    try:
        if key_type == "hash":
            return parse_reactions(r.hgetall(key))
        if key_type == "string":
            return parse_reactions(json.loads(r.get(key) or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def event_ids_with_title(title: str) -> list[str]:
    ids = []
    for doc in get_mongo_database().events.find({}):
        if event_title(doc) == title:
            eid = doc_id(doc)
            if eid:
                ids.append(eid)
    return ids


def reactions_from_cassandra(title: str) -> dict[str, int]:
    counts = empty_reactions()
    session = get_cassandra_session()
    for event_id in event_ids_with_title(title):
        rows = cassandra_execute(session, "SELECT like_value FROM event_reactions WHERE event_id = %s", (event_id,))
        for row in rows:
            if row.like_value == LIKE_VALUE:
                counts["likes"] += 1
            elif row.like_value == DISLIKE_VALUE:
                counts["dislikes"] += 1
    return counts


def reactions_for_title(r: redis_lib.Redis, title: str) -> dict[str, int]:
    key = reaction_cache_key(title)
    cached = cached_reactions(r, key)
    if cached is not None:
        return cached
    reactions = reactions_from_cassandra(title)
    if reactions["likes"] or reactions["dislikes"]:
        cache_reactions(r, key, reactions)
    return reactions


def cache_reactions(r: redis_lib.Redis, key: str, reactions: dict[str, int]) -> None:
    pipe = r.pipeline()
    pipe.delete(key)
    pipe.hset(key, mapping={"likes": reactions["likes"], "dislikes": reactions["dislikes"]})
    pipe.expire(key, get_like_ttl())
    pipe.execute()


def cache_reactions_for_title(r: redis_lib.Redis, title: str) -> None:
    reactions = reactions_from_cassandra(title)
    cache_reactions(r, reaction_cache_key(title), reactions)


def reactions_by_title(docs: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    r = get_redis()
    return {title: reactions_for_title(r, title) for title in {event_title(doc) for doc in docs}}


def review_cache_key(title: str) -> str:
    return f"event:{hashlib.md5(title.encode()).hexdigest()}:reviews"


def empty_reviews_summary() -> dict[str, Any]:
    return {"count": 0, "rating": 0.0}


def parse_reviews_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {"count": int(data.get("count", 0)), "rating": float(data.get("rating", 0.0))}


def cached_reviews_summary(r: redis_lib.Redis, key: str) -> Optional[dict[str, Any]]:
    if r.type(key) != "hash":
        return None
    try:
        return parse_reviews_summary(r.hgetall(key))
    except (TypeError, ValueError):
        return None


def round_rating(total: int, count: int) -> float:
    if count == 0:
        return 0.0
    value = Decimal(total) / Decimal(count)
    return float(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def reviews_from_cassandra(title: str) -> dict[str, Any]:
    count = 0
    total = 0
    session = get_cassandra_session()
    for event_id in event_ids_with_title(title):
        rows = cassandra_execute(session, "SELECT rating FROM event_reviews WHERE event_id = %s", (event_id,))
        for row in rows:
            count += 1
            total += int(row.rating)
    return {"count": count, "rating": round_rating(total, count)}


def cache_reviews_summary(r: redis_lib.Redis, key: str, reviews: dict[str, Any]) -> None:
    pipe = r.pipeline()
    pipe.delete(key)
    pipe.hset(key, mapping={"count": reviews["count"], "rating": reviews["rating"]})
    pipe.expire(key, get_event_reviews_ttl())
    pipe.execute()


def reviews_for_title(r: redis_lib.Redis, title: str) -> dict[str, Any]:
    key = review_cache_key(title)
    cached = cached_reviews_summary(r, key)
    if cached is not None:
        return cached
    reviews = reviews_from_cassandra(title)
    if reviews["count"]:
        cache_reviews_summary(r, key, reviews)
    return reviews


def cache_reviews_for_title(r: redis_lib.Redis, title: str) -> None:
    cache_reviews_summary(r, review_cache_key(title), reviews_from_cassandra(title))


def reviews_by_title(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    r = get_redis()
    return {title: reviews_for_title(r, title) for title in {event_title(doc) for doc in docs}}


def public_events(docs: list[dict[str, Any]], include: Optional[str]) -> list[dict[str, Any]]:
    need_reactions = include_reactions(include)
    need_reviews = include_reviews(include)
    if not need_reactions and not need_reviews:
        return [event_public(d) for d in docs]
    reaction_map = reactions_by_title(docs) if need_reactions else {}
    review_map = reviews_by_title(docs) if need_reviews else {}
    return [
        event_public(
            d,
            reaction_map.get(event_title(d), empty_reactions()) if need_reactions else None,
            review_map.get(event_title(d), empty_reviews_summary()) if need_reviews else None,
        )
        for d in docs
    ]


def upsert_neo4j_user(user_id: str) -> None:
    with get_neo4j_driver().session() as session:
        session.run("MERGE (:User {id: $id})", id=user_id)


def upsert_neo4j_event(event_id: str, title: str) -> None:
    with get_neo4j_driver().session() as session:
        session.run("MERGE (e:Event {id: $id}) SET e.title = $title", id=event_id, title=title)


def save_neo4j_like(user_id: str, event: dict[str, Any]) -> None:
    event_id = doc_id(event)
    with get_neo4j_driver().session() as session:
        session.run(
            """
            MERGE (u:User {id: $user_id})
            MERGE (e:Event {id: $event_id})
            SET e.title = $title
            MERGE (u)-[:LIKED]->(e)
            """,
            user_id=user_id,
            event_id=event_id,
            title=event_title(event),
        )


def recommendation_event_ids(user_id: str) -> list[str]:
    with get_neo4j_driver().session() as session:
        rows = session.run(
            """
            MATCH (me:User {id: $user_id})-[:LIKED]->(:Event)<-[:LIKED]-(other:User)-[:LIKED]->(event:Event)
            WHERE NOT (me)-[:LIKED]->(event)
            RETURN event.id AS id, count(*) AS score
            ORDER BY score DESC
            """,
            user_id=user_id,
        )
        return [row["id"] for row in rows if row["id"]]


def started_sort_key(doc: dict[str, Any]) -> datetime:
    return as_utc_datetime(event_value(doc, "started_at")) or datetime.max.replace(tzinfo=timezone.utc)


def recommended_events_from_neo4j(user_id: str) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for event_id in recommendation_event_ids(user_id):
        doc = find_event(event_id)
        if not doc:
            continue
        title = event_title(doc)
        current = selected.get(title)
        if current is None or started_sort_key(doc) < started_sort_key(current):
            selected[title] = doc
    return [event_public(doc) for doc in selected.values()]


def recommendation_cache_key(user_id: str) -> str:
    return f"user:{user_id}:recomms"


def cached_recommendations(r: redis_lib.Redis, user_id: str) -> Optional[dict[str, Any]]:
    key = recommendation_cache_key(user_id)
    if r.type(key) != "hash":
        return None
    raw = r.hget(key, "events")
    if raw is None:
        return None
    try:
        events = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(events, list):
        return None
    return {"events": events}


def cache_recommendations(r: redis_lib.Redis, user_id: str, data: dict[str, Any]) -> None:
    key = recommendation_cache_key(user_id)
    pipe = r.pipeline()
    pipe.delete(key)
    pipe.hset(key, "events", json.dumps(data["events"]))
    pipe.expire(key, get_recommendations_ttl())
    pipe.execute()


def find_user(raw_id: str) -> Optional[dict[str, Any]]:
    for doc in get_mongo_database().users.find({}):
        if has_id(doc, raw_id):
            return doc
    return None


def find_event(raw_id: str) -> Optional[dict[str, Any]]:
    for doc in get_mongo_database().events.find({}):
        if has_id(doc, raw_id):
            return doc
    return None


def same_created_by(doc: dict[str, Any], user_id: str) -> bool:
    return str(event_value(doc, "created_by")) == user_id


def event_started_in_range(
    doc: dict[str, Any],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
) -> bool:
    started = as_utc_datetime(event_value(doc, "started_at"))
    if not started:
        return False
    if date_from and started < date_from:
        return False
    if date_to and started >= date_to:
        return False
    return True


def parse_event_query(
    *,
    limit: Optional[str],
    offset: Optional[str],
    raw_id: Optional[str],
    category: Optional[str],
    price_from: Optional[str],
    price_to: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    started_date_from: Optional[str],
    started_date_to: Optional[str],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    lim, le = parse_uint_q(limit, None)
    if le:
        return None, "limit"
    off, oe = parse_uint_q(offset, 0)
    if oe:
        return None, "offset"
    if raw_id is not None and not raw_id:
        return None, "id"
    if category is not None and category not in CATEGORIES:
        return None, "category"
    pf, pfe = parse_uint_q(price_from, None)
    if pfe:
        return None, "price_from"
    pt, pte = parse_uint_q(price_to, None)
    if pte:
        return None, "price_to"
    raw_from = date_from if date_from is not None else started_date_from
    raw_to = date_to if date_to is not None else started_date_to
    df, dfe = parse_day_q(raw_from)
    if dfe:
        return None, "date_from" if date_from is not None else "started_date_from"
    dt, dte = parse_day_q(raw_to)
    if dte:
        return None, "date_to" if date_to is not None else "started_date_to"
    if dt:
        dt += timedelta(days=1)
    return {
        "limit": lim,
        "offset": off or 0,
        "id": raw_id,
        "category": category,
        "price_from": pf,
        "price_to": pt,
        "date_from": df,
        "date_to": dt,
    }, None


def filter_events(
    docs: list[dict[str, Any]],
    *,
    filters: dict[str, Any],
    title: Optional[str] = None,
    city: Optional[str] = None,
    created_by_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    result = []
    title_lc = title.lower() if title else None
    for doc in docs:
        if filters["id"] and not has_id(doc, filters["id"]):
            continue
        if title_lc and title_lc not in str(event_value(doc, "title")).lower():
            continue
        if filters["category"] and event_value(doc, "category") != filters["category"]:
            continue
        price = event_value(doc, "price")
        if filters["price_from"] is not None or filters["price_to"] is not None:
            try:
                price = int(price)
            except (TypeError, ValueError):
                continue
            if filters["price_from"] is not None and price < filters["price_from"]:
                continue
            if filters["price_to"] is not None and price > filters["price_to"]:
                continue
        location = doc.get("location") or {}
        if city is not None and location.get("city") != city:
            continue
        if created_by_ids is not None and str(event_value(doc, "created_by")) not in created_by_ids:
            continue
        if (filters["date_from"] or filters["date_to"]) and not event_started_in_range(
            doc,
            filters["date_from"],
            filters["date_to"],
        ):
            continue
        result.append(doc)
    return result


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
async def users_register(request: Request, x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    r = get_redis()
    data, bad = await parse_json_body(request)
    if bad:
        out = body({"message": f'invalid "{bad}" field'}, 400)
        touch_session_post(r, x_session_id, out)
        return out
    for field in ("full_name", "username", "password"):
        if not non_empty_str(data.get(field)):
            out = body({"message": f'invalid "{field}" field'}, 400)
            touch_session_post(r, x_session_id, out)
            return out
    doc = {
        "full_name": data["full_name"].strip(),
        "username": data["username"].strip(),
        "password_hash": bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode(),
    }
    try:
        ins = get_mongo_database().users.insert_one(doc)
    except DuplicateKeyError:
        out = body({"message": "user already exists"}, 409)
        touch_session_post(r, x_session_id, out)
        return out
    upsert_neo4j_user(str(ins.inserted_id))
    out = Response(status_code=201)
    set_session_cookie(out, create_fresh_session(r, str(ins.inserted_id)))
    return out


@app.post("/auth/login")
async def auth_login(request: Request, x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    r = get_redis()
    data, bad = await parse_json_body(request)
    if bad:
        out = body({"message": f'invalid "{bad}" field'}, 400)
        touch_session_post(r, x_session_id, out)
        return out
    for field in ("username", "password"):
        if not non_empty_str(data.get(field)):
            out = body({"message": f'invalid "{field}" field'}, 400)
            touch_session_post(r, x_session_id, out)
            return out
    u = get_mongo_database().users.find_one({"username": data["username"].strip()})
    if not u or not bcrypt.checkpw(data["password"].encode(), u["password_hash"].encode()):
        out = body({"message": "invalid credentials"}, 401)
        touch_session_post(r, x_session_id, out)
        return out
    uid = str(u["_id"])
    out = Response(status_code=204)
    sk = f"sid:{x_session_id}" if x_session_id and is_valid_sid(x_session_id) else ""
    if sk and r.exists(sk):
        pipe = r.pipeline()
        pipe.hset(sk, mapping={"user_id": uid, "updated_at": redis_ts()})
        pipe.expire(sk, get_ttl())
        pipe.execute()
        set_session_cookie(out, x_session_id)
    else:
        set_session_cookie(out, create_fresh_session(r, uid))
    return out


@app.post("/auth/logout")
async def auth_logout(x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    r = get_redis()
    key = f"sid:{x_session_id}" if x_session_id and is_valid_sid(x_session_id) else ""
    if not key or not r.exists(key):
        return Response(status_code=401)
    r.delete(key)
    out = Response(status_code=204)
    clear_session_cookie(out)
    return out


@app.post("/events")
async def events_create(request: Request, x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    r = get_redis()
    uid = session_user_id(r, x_session_id)
    if not uid:
        out = Response(status_code=401)
        touch_session_post(r, x_session_id, out)
        return out
    data, bad = await parse_json_body(request)
    if bad:
        out = body({"message": f'invalid "{bad}" field'}, 400)
        touch_session_post(r, x_session_id, out)
        return out
    for field in ("title", "address", "started_at", "finished_at", "description"):
        if field in ("started_at", "finished_at"):
            if not parse_rfc3339_tz(data.get(field, "")):
                out = body({"message": f'invalid "{field}" field'}, 400)
                touch_session_post(r, x_session_id, out)
                return out
        elif not non_empty_str(data.get(field)):
            out = body({"message": f'invalid "{field}" field'}, 400)
            touch_session_post(r, x_session_id, out)
            return out
    try:
        ObjectId(uid)
    except InvalidId:
        out = Response(status_code=401)
        touch_session_post(r, x_session_id, out)
        return out
    doc = {
        "title": data["title"].strip(),
        "description": data["description"].strip(),
        "location": {"address": data["address"].strip()},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "created_by": uid,
        "started_at": data["started_at"].strip(),
        "finished_at": data["finished_at"].strip(),
    }
    try:
        ins = get_mongo_database().events.insert_one(doc)
    except DuplicateKeyError:
        out = body({"message": "event already exists"}, 409)
        touch_session_post(r, x_session_id, out)
        return out
    upsert_neo4j_event(str(ins.inserted_id), event_title(doc))
    out = body({"id": str(ins.inserted_id)}, 201)
    touch_session_post(r, x_session_id, out)
    return out


@app.get("/events")
async def events_list(
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
    title: Optional[str] = Query(None),
    limit: Optional[str] = Query(None),
    offset: Optional[str] = Query(None),
    id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    price_from: Optional[str] = Query(None),
    price_to: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    started_date_from: Optional[str] = Query(None),
    started_date_to: Optional[str] = Query(None),
    user: Optional[str] = Query(None),
    include: Optional[str] = Query(None),
):
    filters, bad = parse_event_query(
        limit=limit,
        offset=offset,
        raw_id=id,
        category=category,
        price_from=price_from,
        price_to=price_to,
        date_from=date_from,
        date_to=date_to,
        started_date_from=started_date_from,
        started_date_to=started_date_to,
    )
    if bad:
        out = invalid_field(bad)
        echo_session_get(out, x_session_id)
        return out
    if city is not None and not city:
        out = invalid_field("city")
        echo_session_get(out, x_session_id)
        return out
    if user is not None and not user:
        out = invalid_field("user")
        echo_session_get(out, x_session_id)
        return out

    created_by_ids = None
    if user is not None:
        created_by_ids = set()
        for u in get_mongo_database().users.find({"username": user}):
            created_by_ids.update(doc_ids(u))
    rows = filter_events(
        list(get_mongo_database().events.find({})),
        filters=filters,
        title=title,
        city=city,
        created_by_ids=created_by_ids,
    )
    rows = rows[filters["offset"] :]
    if filters["limit"] is not None:
        rows = rows[: filters["limit"]]
    events = public_events(rows, include)
    out = Response(
        content=json.dumps({"events": events, "count": len(events)}),
        media_type=JSON,
        status_code=200,
    )
    echo_session_get(out, x_session_id)
    return out


@app.get("/events/{event_id}")
async def events_get(
    event_id: str,
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
    include: Optional[str] = Query(None),
):
    event = find_event(event_id)
    if not event:
        out = body({"message": "Not found"}, 404)
        echo_session_get(out, x_session_id)
        return out
    reactions = None
    reviews = None
    if include_reactions(include):
        reactions = reactions_for_title(get_redis(), event_title(event))
    if include_reviews(include):
        reviews = reviews_for_title(get_redis(), event_title(event))
    out = body(event_public(event, reactions, reviews), 200)
    echo_session_get(out, x_session_id)
    return out


def save_event_reaction(event: dict[str, Any], user_id: str, like_value: int, r: redis_lib.Redis) -> None:
    event_id = doc_id(event)
    cassandra_execute(
        get_cassandra_session(),
        (
            "INSERT INTO event_reactions (event_id, like_value, created_by, created_at) "
            "VALUES (%s, %s, %s, %s)"
        ),
        (event_id, like_value, user_id, datetime.now(timezone.utc)),
    )
    cache_reactions_for_title(r, event_title(event))
    if like_value == LIKE_VALUE:
        save_neo4j_like(user_id, event)


def react_to_event(
    event_id: str,
    like_value: int,
    x_session_id: Optional[str],
    *,
    clear_unauthorized: bool = False,
) -> Response:
    r = get_redis()
    uid = session_user_id(r, x_session_id)
    if not uid:
        out = Response(status_code=401)
        if clear_unauthorized:
            clear_session_cookie(out)
        return out
    event = find_event(event_id)
    if not event:
        out = body({"message": "Event not found"}, 404)
        touch_session_post(r, x_session_id, out)
        return out
    save_event_reaction(event, uid, like_value, r)
    out = Response(status_code=204)
    touch_session_post(r, x_session_id, out)
    return out


@app.post("/events/{event_id}/like")
async def events_like(event_id: str, x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    return react_to_event(event_id, LIKE_VALUE, x_session_id)


@app.post("/events/{event_id}/dislike")
async def events_dislike(event_id: str, x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    return react_to_event(event_id, DISLIKE_VALUE, x_session_id, clear_unauthorized=True)


def valid_rating(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= 5


def valid_comment(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 300


def review_public(row) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "event_id": row.event_id,
        "comment": row.comment,
        "created_at": format_dt(row.created_at),
        "created_by": row.created_by,
        "rating": int(row.rating),
        "updated_at": format_dt(row.updated_at),
    }


def find_user_review(event_id: str, user_id: str):
    rows = cassandra_execute(
        get_cassandra_session(),
        "SELECT id, event_id, rating, comment, created_at, created_by, updated_at FROM event_reviews WHERE event_id = %s AND created_by = %s",
        (event_id, user_id),
    )
    return rows.one()


@app.post("/events/{event_id}/reviews")
async def events_reviews_create(
    event_id: str,
    request: Request,
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    r = get_redis()
    uid = session_user_id(r, x_session_id)
    if not uid:
        return Response(status_code=401)
    data, bad = await parse_json_body(request)
    if bad:
        out = invalid_field(bad)
        touch_session_post(r, x_session_id, out)
        return out
    if not valid_comment(data.get("comment")):
        out = invalid_field("comment")
        touch_session_post(r, x_session_id, out)
        return out
    if not valid_rating(data.get("rating")):
        out = invalid_field("rating")
        touch_session_post(r, x_session_id, out)
        return out
    event = find_event(event_id)
    if not event:
        out = body({"message": "Event not found"}, 404)
        touch_session_post(r, x_session_id, out)
        return out
    if find_user_review(event_id, uid):
        out = body({"message": "Already exists"}, 409)
        touch_session_post(r, x_session_id, out)
        return out

    review_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    cassandra_execute(
        get_cassandra_session(),
        (
            "INSERT INTO event_reviews (event_id, created_by, id, rating, comment, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        ),
        (event_id, uid, review_id, int(data["rating"]), data["comment"], now, now),
    )
    cache_reviews_for_title(r, event_title(event))
    out = body({"id": str(review_id)}, 201)
    touch_session_post(r, x_session_id, out)
    return out


@app.get("/events/{event_id}/reviews")
async def events_reviews_list(
    event_id: str,
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
    limit: Optional[str] = Query(None),
    offset: Optional[str] = Query(None),
):
    lim, le = parse_uint_q(limit, None)
    if le:
        out = invalid_field("limit")
        echo_session_get(out, x_session_id)
        return out
    off, oe = parse_uint_q(offset, 0)
    if oe:
        out = invalid_field("offset")
        echo_session_get(out, x_session_id)
        return out
    rows = list(
        cassandra_execute(
            get_cassandra_session(),
            "SELECT id, event_id, rating, comment, created_at, created_by, updated_at FROM event_reviews WHERE event_id = %s",
            (event_id,),
        )
    )
    rows.sort(key=lambda row: row.created_at, reverse=True)
    rows = rows[off or 0 :]
    if lim is not None:
        rows = rows[:lim]
    reviews = [review_public(row) for row in rows]
    out = body({"reviews": reviews, "count": len(reviews)}, 200)
    echo_session_get(out, x_session_id)
    return out


@app.patch("/events/{event_id}/reviews/{review_id}")
async def events_reviews_update(
    event_id: str,
    review_id: str,
    request: Request,
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    r = get_redis()
    uid = session_user_id(r, x_session_id)
    if not uid:
        return Response(status_code=401)
    event = find_event(event_id)
    if not event:
        out = body({"message": "Event not found"}, 404)
        touch_session_post(r, x_session_id, out)
        return out
    data, bad = await parse_json_body(request)
    if bad:
        out = invalid_field(bad)
        touch_session_post(r, x_session_id, out)
        return out
    update: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if "comment" in data:
        if not valid_comment(data["comment"]):
            out = invalid_field("comment")
            touch_session_post(r, x_session_id, out)
            return out
        update["comment"] = data["comment"]
    if "rating" in data:
        if not valid_rating(data["rating"]):
            out = invalid_field("rating")
            touch_session_post(r, x_session_id, out)
            return out
        update["rating"] = int(data["rating"])
    review = find_user_review(event_id, uid)
    if not review or str(review.id) != review_id:
        out = body({"message": "Event not found"}, 404)
        touch_session_post(r, x_session_id, out)
        return out
    assignments = ", ".join(f"{name} = %s" for name in update)
    params = tuple(update.values()) + (event_id, uid)
    cassandra_execute(
        get_cassandra_session(),
        f"UPDATE event_reviews SET {assignments} WHERE event_id = %s AND created_by = %s",
        params,
    )
    cache_reviews_for_title(r, event_title(event))
    out = Response(status_code=204)
    touch_session_post(r, x_session_id, out)
    return out


@app.patch("/events/{event_id}")
async def events_update(
    event_id: str,
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
        out = invalid_field(bad)
        touch_session_post(r, x_session_id, out)
        return out

    update_set: dict[str, Any] = {}
    update_unset: dict[str, str] = {}
    if "category" in data:
        if data["category"] not in CATEGORIES:
            out = invalid_field("category")
            touch_session_post(r, x_session_id, out)
            return out
        update_set["category"] = data["category"]
    if "price" in data:
        if not parse_uint_body(data["price"]):
            out = invalid_field("price")
            touch_session_post(r, x_session_id, out)
            return out
        update_set["price"] = data["price"]
    if "city" in data:
        if not isinstance(data["city"], str):
            out = invalid_field("city")
            touch_session_post(r, x_session_id, out)
            return out
        if data["city"] == "":
            update_unset["location.city"] = ""
        else:
            update_set["location.city"] = data["city"]

    event = find_event(event_id)
    if not event or not same_created_by(event, uid):
        out = body({"message": "Not found. Be sure that event exists and you are the organizer"}, 404)
        touch_session_post(r, x_session_id, out)
        return out

    ops: dict[str, Any] = {}
    if update_set:
        ops["$set"] = update_set
    if update_unset:
        ops["$unset"] = update_unset
    if ops:
        get_mongo_database().events.update_one({"_id": event["_id"]}, ops)
    out = Response(status_code=204)
    touch_session_post(r, x_session_id, out)
    return out


@app.get("/recommendations")
async def recommendations(x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    r = get_redis()
    uid = session_user_id(r, x_session_id)
    if not uid:
        return Response(status_code=401)
    cached = cached_recommendations(r, uid)
    if cached is not None:
        out = body(cached, 200)
        touch_session_post(r, x_session_id, out)
        return out
    data = {"events": recommended_events_from_neo4j(uid)}
    cache_recommendations(r, uid, data)
    out = body(data, 200)
    touch_session_post(r, x_session_id, out)
    return out


@app.get("/users")
async def users_list(
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
    limit: Optional[str] = Query(None),
    offset: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    id: Optional[str] = Query(None),
):
    lim, le = parse_uint_q(limit, None)
    if le:
        out = invalid_field("limit")
        echo_session_get(out, x_session_id)
        return out
    off, oe = parse_uint_q(offset, 0)
    if oe:
        out = invalid_field("offset")
        echo_session_get(out, x_session_id)
        return out
    if id is not None and not id:
        out = invalid_field("id")
        echo_session_get(out, x_session_id)
        return out
    if name is not None and not name:
        out = invalid_field("name")
        echo_session_get(out, x_session_id)
        return out

    rows = []
    name_lc = name.lower() if name else None
    for doc in get_mongo_database().users.find({}):
        if id and not has_id(doc, id):
            continue
        if name_lc and name_lc not in str(doc.get("full_name", "")).lower():
            continue
        rows.append(doc)
    rows = rows[off or 0 :]
    if lim is not None:
        rows = rows[:lim]
    users = [user_public(d) for d in rows]
    out = body({"users": users, "count": len(users)}, 200)
    echo_session_get(out, x_session_id)
    return out


@app.get("/users/{user_id}/events")
async def users_events(
    user_id: str,
    x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
    title: Optional[str] = Query(None),
    limit: Optional[str] = Query(None),
    offset: Optional[str] = Query(None),
    id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    price_from: Optional[str] = Query(None),
    price_to: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    started_date_from: Optional[str] = Query(None),
    started_date_to: Optional[str] = Query(None),
    include: Optional[str] = Query(None),
):
    user_doc = find_user(user_id)
    if not user_doc:
        out = body({"message": "User not found"}, 404)
        echo_session_get(out, x_session_id)
        return out
    filters, bad = parse_event_query(
        limit=limit,
        offset=offset,
        raw_id=id,
        category=category,
        price_from=price_from,
        price_to=price_to,
        date_from=date_from,
        date_to=date_to,
        started_date_from=started_date_from,
        started_date_to=started_date_to,
    )
    if bad:
        out = invalid_field(bad)
        echo_session_get(out, x_session_id)
        return out
    if city is not None and not city:
        out = invalid_field("city")
        echo_session_get(out, x_session_id)
        return out
    rows = filter_events(
        list(get_mongo_database().events.find({})),
        filters=filters,
        title=title,
        city=city,
        created_by_ids=doc_ids(user_doc),
    )
    rows = rows[filters["offset"] :]
    if filters["limit"] is not None:
        rows = rows[: filters["limit"]]
    events = public_events(rows, include)
    out = body({"events": events, "count": len(events)}, 200)
    echo_session_get(out, x_session_id)
    return out


@app.get("/users/{user_id}")
async def users_get(user_id: str, x_session_id: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    user_doc = find_user(user_id)
    if not user_doc:
        out = body({"message": "Not found"}, 404)
        echo_session_get(out, x_session_id)
        return out
    out = body(user_public(user_doc), 200)
    echo_session_get(out, x_session_id)
    return out


def run():
    host = os.environ["APP_HOST"]
    port = int(os.environ["APP_PORT"])
    uvicorn.run(app, host=host, port=port)
