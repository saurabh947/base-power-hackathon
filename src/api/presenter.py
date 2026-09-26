"""Presenter passcode (the one exception to "no auth"): only the presenter may crash controllers.

The passcode is read at request time, so rotating the secret needs no restart:
  1. the file at env PRESENTER_PASSCODE_FILE (default /secrets/presenter/passcode,
     where Cloud Run mounts the Secret Manager secret), else
  2. env PRESENTER_PASSCODE (local dev; DEMO_PRESENTER_PASSCODE is accepted as an alias).
If neither is set, presenter endpoints answer 503 presenter_passcode_not_set.
Comparison is constant-time over SHA-256 digests, so length is not leaked either.
"""
from __future__ import annotations

import hashlib
import hmac
import os

DEFAULT_FILE = "/secrets/presenter/passcode"


def configured_passcode() -> str | None:
    path = os.environ.get("PRESENTER_PASSCODE_FILE", DEFAULT_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value
    except OSError:
        pass
    for var in ("PRESENTER_PASSCODE", "DEMO_PRESENTER_PASSCODE"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return None


def matches(provided: str | None, expected: str) -> bool:
    given = hashlib.sha256((provided or "").encode("utf-8")).digest()
    want = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(given, want) and provided is not None
