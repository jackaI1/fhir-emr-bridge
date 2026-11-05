"""Offline operator commands. Stop the service before changing wrapping keys."""

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

from .crypto import PrivacyError, encode
from .keyfile import create_keys, load_keys
from .vault import Vault


def add_key(path: Path, kid: str):
    load_keys(path)  # Validate format and private permissions before editing.
    data = json.loads(path.read_text())
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", kid) or kid in data["keys"]:
        raise PrivacyError("Choose a new key ID using 1-64 letters, digits, underscores or hyphens")
    data["keys"][kid] = encode(secrets.token_bytes(32))
    data["active"] = kid
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".bridge-key-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["init", "add-key", "rotate", "verify", "checkpoint", "purge"]
    )
    parser.add_argument("--keys", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            create_keys(args.keys)
            print("Created private local key file; keep it separate from the database and git.")
        elif args.command == "add-key":
            if not args.id:
                raise PrivacyError("add-key requires --id")
            add_key(args.keys, args.id)
            print("Added active wrapping key. Next run rotate with the service stopped.")
        else:
            if not args.db or not args.db.is_file():
                raise PrivacyError("An existing --db is required")
            with Vault(args.db, *load_keys(args.keys)) as vault:
                if args.command == "rotate":
                    print(json.dumps({"rotated_records": vault.rotate(vault.cipher)}))
                elif args.command == "purge":
                    print(json.dumps({"purged_records": vault.purge()}))
                elif args.command == "verify":
                    checkpoint = (
                        json.loads(args.checkpoint.read_text()) if args.checkpoint else None
                    )
                    print(json.dumps(vault.verify(checkpoint)))
                else:
                    if not args.output:
                        raise PrivacyError(
                            "checkpoint requires --output in a separate trusted location"
                        )
                    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as output:
                        json.dump(vault.checkpoint(), output)
                        output.flush()
                        os.fsync(output.fileno())
                    print("Saved signed audit checkpoint; preserve it outside the database host.")
        return 0
    except (PrivacyError, OSError, ValueError, TypeError) as exc:
        message = (
            str(exc)
            if isinstance(exc, PrivacyError)
            else "Operator command failed; check private paths and inputs"
        )
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
