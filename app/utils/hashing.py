from __future__ import annotations

import hashlib


def sha256_hex(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(value)
    return digest.hexdigest()
