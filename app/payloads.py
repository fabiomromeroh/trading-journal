"""Compact JSON stored in text columns.

New writes are ``gz:`` + base64(gzip(json)) when that is smaller than the plain JSON.
Reads accept that form, raw gzip bytes, and the old uncompressed JSON rows.
"""
from __future__ import annotations

import base64
import gzip
import json


def pack_json(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":"), default=str).encode()
    packed = "gz:" + base64.b64encode(gzip.compress(raw, compresslevel=6)).decode("ascii")
    if len(packed) >= len(raw):
        return raw.decode()
    return packed


def unpack_json(text, object_hook=None):
    if isinstance(text, (bytes, bytearray)):
        if text[:2] == b"\x1f\x8b":
            text = gzip.decompress(bytes(text)).decode()
        else:
            text = bytes(text).decode()
    elif isinstance(text, str) and text.startswith("gz:"):
        text = gzip.decompress(base64.b64decode(text[3:])).decode()
    return json.loads(text, object_hook=object_hook)


def dialect_insert(db):
    """INSERT that supports ON CONFLICT on the databases this app runs (SQLite and Postgres)."""
    name = db.get_bind().dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert
