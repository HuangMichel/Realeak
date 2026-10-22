import os
import re
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", encoding="utf-8-sig")


def _url() -> str:
    u = os.environ.get("DATABASE_URL") or f"sqlite:///{(ROOT / 'data.db').as_posix()}"
    if u.startswith("postgres://"):
        u = "postgresql+psycopg://" + u[len("postgres://"):]
    elif u.startswith("postgresql://"):
        u = "postgresql+psycopg://" + u[len("postgresql://"):]
    if u.startswith("postgresql+psycopg://"):
        u = re.sub(r"([?&])uselibpqcompat=[^&]*&?", r"\1", u).rstrip("?&")
    return u


URL = _url()
_kw = {"connect_args": {"check_same_thread": False}} if URL.startswith("sqlite") else {}
engine = create_engine(URL, pool_pre_ping=True, **_kw)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS products(
        id TEXT PRIMARY KEY, brand TEXT, title TEXT, product_type TEXT, handle TEXT,
        price_eur REAL, image_url TEXT, images TEXT, tags TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS listings(
        id TEXT PRIMARY KEY, brand TEXT, country TEXT, title TEXT, condition TEXT,
        condition_norm TEXT, price_eur REAL, price_with_fees REAL, image_url TEXT,
        source_url TEXT, first_seen TEXT, last_seen TEXT, run_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS matches(
        listing_id TEXT PRIMARY KEY, product_id TEXT, confidence REAL, method TEXT,
        reasoning TEXT, steps INTEGER, run_id TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS memory(
        id TEXT PRIMARY KEY, brand TEXT, kind TEXT, key TEXT, value TEXT,
        hits INTEGER DEFAULT 0, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS runs(
        id TEXT PRIMARY KEY, brand TEXT, kind TEXT, started_at TEXT, finished_at TEXT,
        status TEXT, stats TEXT, log TEXT)""",
    """CREATE TABLE IF NOT EXISTS labels(
        listing_id TEXT PRIMARY KEY, product_id TEXT, note TEXT, created_at TEXT)""",
]

MIGRATIONS = [
    "ALTER TABLE matches ADD COLUMN category TEXT",
    "ALTER TABLE labels ADD COLUMN category TEXT",
]


def init_db() -> None:
    with engine.begin() as c:
        for s in SCHEMA:
            c.execute(text(s))
    for s in MIGRATIONS:  # best-effort : la colonne existe peut-etre deja (SQLite et Postgres)
        try:
            with engine.begin() as c:
                c.execute(text(s))
        except Exception:  # noqa: BLE001
            pass


def q(sql: str, **params) -> list[dict]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


def x(sql: str, **params) -> None:
    with engine.begin() as c:
        c.execute(text(sql), params)
