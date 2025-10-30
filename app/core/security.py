from __future__ import annotations

import json
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


@lru_cache(maxsize=4)
def _get_cipher(key: str) -> Fernet:
    return Fernet(key.encode("utf-8"))


def encrypt_refresh_token(key: str, value: str) -> str:
    cipher = _get_cipher(key)
    token = value.encode("utf-8")
    return cipher.encrypt(token).decode("utf-8")


def decrypt_refresh_token(key: str, value: str) -> str:
    cipher = _get_cipher(key)
    try:
        decrypted = cipher.decrypt(value.encode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("Invalid refresh token payload") from exc
    return decrypted.decode("utf-8")


def mask_secret(value: Optional[str], visible: int = 4) -> str:
    if not value:
        return "[redacted]"
    trimmed = value.strip()
    if len(trimmed) <= visible:
        return "*" * len(trimmed)
    return f"{trimmed[:visible]}***"


def encode_oauth_state(key: str, payload: dict[str, str]) -> str:
    cipher = _get_cipher(key)
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return cipher.encrypt(serialized).decode("utf-8")


def decode_oauth_state(key: str, token: str) -> dict[str, str]:
    cipher = _get_cipher(key)
    try:
        decrypted = cipher.decrypt(token.encode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("Invalid OAuth state token") from exc
    data = json.loads(decrypted.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid OAuth state payload")
    return {str(k): str(v) for k, v in data.items()}
