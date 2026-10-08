from __future__ import annotations

import base64
import hashlib
import time
from collections import defaultdict

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    # Accept any secret string; derive a valid Fernet key from it.
    derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
    return Fernet(derived)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Could not decrypt stored token (TOKEN_ENCRYPTION_KEY changed?)") from exc


class LoginThrottle:
    """Tiny in-memory brute-force guard: max 5 failures per 15 minutes per IP."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window = limit, window
        self.failures: dict[str, list[float]] = defaultdict(list)

    def blocked(self, ip: str) -> bool:
        now = time.time()
        self.failures[ip] = [t for t in self.failures[ip] if now - t < self.window]
        return len(self.failures[ip]) >= self.limit

    def fail(self, ip: str) -> None:
        self.failures[ip].append(time.time())

    def reset(self, ip: str) -> None:
        self.failures.pop(ip, None)


throttle = LoginThrottle()
