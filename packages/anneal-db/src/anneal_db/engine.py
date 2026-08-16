"""DB engine + token mint. Sync SQLAlchemy Core over the raw-SQL schema (§3) — repositories use
`text()` parameterized queries so the concurrency primitives run exactly as LLD §3.1 writes them
(no ORM/DDL skew). v1 laptop scale: brief sync DB calls from the async scheduler are acceptable.
"""
from __future__ import annotations

import os
import secrets
import string
from functools import lru_cache

from sqlalchemy import URL, Engine, create_engine

DEFAULT_URL = "postgresql+psycopg://anneal:anneal@localhost:55432/anneal"
_TOKEN_ALPHABET = string.ascii_lowercase + string.digits   # ^[a-z0-9]{10}$ — regex/\b-safe (B1/§8)


def db_url() -> str:
    # ANNEAL_DB_URL wins. Else assemble from split DB_* vars (NDP managed-database injection);
    # URL.create escapes password special chars. postgres-only app → driver is fixed, DB_ENGINE
    # is ignored (ponytail: single-engine app; branch on DB_ENGINE if a second driver ever lands).
    if url := os.getenv("ANNEAL_DB_URL"):
        return url
    if host := os.getenv("DB_HOST"):
        port = os.getenv("DB_PORT")
        return URL.create(
            "postgresql+psycopg",
            username=os.getenv("DB_USERNAME"),
            password=os.getenv("DB_PASSWORD"),
            host=host,
            port=int(port) if port else None,
            database=os.getenv("DB_NAME"),
        ).render_as_string(hide_password=False)
    return DEFAULT_URL


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(db_url(), future=True, pool_pre_ping=True)


def mint_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(10))
