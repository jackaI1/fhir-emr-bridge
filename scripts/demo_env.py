"""Generate uncommitted, per-install demo credentials; never print their values."""

import os
import secrets
from pathlib import Path


def main():
    destination = Path(".env")
    content = "".join(
        f"{name}={secrets.token_urlsafe(24)}\n"
        for name in [
            "DEMO_DB_ROOT_PASSWORD",
            "DEMO_DB_PASSWORD",
            "DEMO_ADMIN_PASSWORD",
        ]
    )
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(content)
    print("Created .env (mode 600). Existing files are never overwritten.")


if __name__ == "__main__":
    main()
