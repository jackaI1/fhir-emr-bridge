"""Local demonstration key provider. Production needs an independently managed KMS."""

import json
import os
import secrets
from pathlib import Path

from .crypto import EnvelopeCipher, PrivacyError, decode, encode


def fresh_keys() -> dict:
    return {
        "active": "v1",
        "keys": {"v1": encode(secrets.token_bytes(32))},
        "audit": encode(secrets.token_bytes(32)),
    }


def load_keys(path: Path | None = None) -> tuple[EnvelopeCipher, bytes]:
    try:
        if path:
            if path.stat().st_mode & 0o077:
                raise PrivacyError("Key file must be private to its owner (mode 600)")
            data = json.loads(path.read_text())
        else:
            data = fresh_keys()
        cipher = EnvelopeCipher(
            {kid: decode(key) for kid, key in data["keys"].items()}, data["active"]
        )
        audit = decode(data["audit"])
        if len(audit) != 32 or audit in cipher.keys.values():
            raise ValueError("Audit key must be separate")
        return cipher, audit
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PrivacyError("Could not load private encryption and audit keys") from exc


def create_keys(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(fresh_keys(), output)
        output.flush()
        os.fsync(output.fileno())
