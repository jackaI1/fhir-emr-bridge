"""Small single-worker encrypted report/token store with transactional audit records."""

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .crypto import EnvelopeCipher, PrivacyError, canonical

GENESIS = "0" * 64


class Vault:
    def __init__(self, path, cipher: EnvelopeCipher, audit_key: bytes, *, clock=time.time):
        if len(audit_key) != 32:
            raise ValueError("A separate 256-bit audit key is required")
        self.cipher, self.audit_key, self.clock = cipher, audit_key, clock
        self.lock = threading.RLock()
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = (
                os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                if not path.exists()
                else None
            )
            if fd is not None:
                os.close(fd)
            if path.stat().st_mode & 0o077:
                raise PrivacyError("Vault file must be private to its owner (mode 600)")
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                namespace TEXT NOT NULL, rid TEXT NOT NULL, actor TEXT NOT NULL,
                expires REAL NOT NULL, fields TEXT NOT NULL, tag TEXT NOT NULL,
                PRIMARY KEY(namespace, rid, actor));
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY, event TEXT NOT NULL,
                previous TEXT NOT NULL, mac TEXT NOT NULL);
        """)
        try:
            self.verify()
        except Exception:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()

    def mac(self, value) -> str:
        return hmac.new(self.audit_key, canonical(value), hashlib.sha256).hexdigest()

    def opaque(self, category: str, value: str) -> str:
        return self.mac(["identifier", category, value])

    def verify(self, checkpoint: dict | None = None) -> dict:
        with self.lock:
            previous, count = GENESIS, 0
            anchor = GENESIS if checkpoint and checkpoint.get("count") == 0 else None
            try:
                for seq, event, prev, mac in self.db.execute(
                    "SELECT seq,event,previous,mac FROM audit ORDER BY seq"
                ):
                    count += 1
                    expected = self.mac(["audit-v1", seq, json.loads(event), prev])
                    if seq != count or prev != previous or not hmac.compare_digest(expected, mac):
                        raise PrivacyError("Audit chain integrity check failed")
                    previous = mac
                    if checkpoint and count == checkpoint["count"]:
                        anchor = mac
                result = {"count": count, "head": previous}
                if checkpoint:
                    signed = {"count": checkpoint["count"], "head": checkpoint["head"]}
                    if anchor != signed["head"] or not hmac.compare_digest(
                        checkpoint["signature"], self.mac(["checkpoint-v1", signed])
                    ):
                        raise PrivacyError("Audit checkpoint does not match this history")
                return result
            except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                raise PrivacyError("Audit chain could not be verified") from exc

    def checkpoint(self) -> dict:
        with self.lock:
            head = self.verify()
            return {**head, "signature": self.mac(["checkpoint-v1", head])}

    def _append(self, action: str, actor: str, reference: str, outcome: str):
        row = self.db.execute("SELECT seq,mac FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        seq, previous = (row[0] + 1, row[1]) if row else (1, GENESIS)
        event = {
            "time": self.clock(),
            "action": action,
            "actor": self.opaque("actor", actor),
            "record": self.opaque("record", reference),
            "outcome": outcome,
        }
        self.db.execute(
            "INSERT INTO audit VALUES (?,?,?,?)",
            (
                seq,
                canonical(event).decode(),
                previous,
                self.mac(["audit-v1", seq, event, previous]),
            ),
        )

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                self.verify()
                yield
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                if isinstance(exc, sqlite3.Error):
                    raise PrivacyError("Private storage or audit is unavailable") from exc
                raise

    def audit(self, action: str, actor="local-demo", reference="", outcome="ok"):
        with self.transaction():
            self._append(action, actor, reference, outcome)

    def put(self, namespace: str, rid: str, fields: dict, *, actor="local-demo", ttl=86400):
        key = (namespace, self.opaque("record", rid), self.opaque("actor", actor))
        expires = self.clock() + ttl
        meta = [*key, expires]
        encrypted = {
            name: self.cipher.seal(value, canonical([meta, name]).decode())
            for name, value in fields.items()
        }
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)",
                (
                    *key,
                    expires,
                    canonical(encrypted).decode(),
                    self.mac(["row-v1", meta, encrypted]),
                ),
            )
            self._append("store:" + namespace, actor, rid, "ok")

    def _row(self, row):
        namespace, rid, actor, expires, fields, tag = row
        try:
            fields = json.loads(fields)
            meta = [namespace, rid, actor, expires]
            if not hmac.compare_digest(tag, self.mac(["row-v1", meta, fields])):
                raise PrivacyError("Stored record integrity check failed")
            return meta, fields
        except (ValueError, TypeError) as exc:
            raise PrivacyError("Stored record could not be authenticated") from exc

    def get(self, namespace: str, rid: str, *, actor="local-demo") -> dict | None:
        result, denied = None, False
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM records WHERE namespace=? AND rid=? AND actor=?",
                (namespace, self.opaque("record", rid), self.opaque("actor", actor)),
            ).fetchone()
            if row:
                meta, fields = self._row(row)
                if meta[-1] > self.clock():
                    result = {
                        name: self.cipher.open(value, canonical([meta, name]).decode())
                        for name, value in fields.items()
                    }
            elif self.db.execute(
                "SELECT 1 FROM records WHERE namespace=? AND rid=?",
                (namespace, self.opaque("record", rid)),
            ).fetchone():
                denied = True
            self._append(
                "read:" + namespace,
                actor,
                rid,
                "denied" if denied else ("ok" if result is not None else "missing"),
            )
        if denied:
            raise PrivacyError("Stored record belongs to another session")
        return result

    def delete(self, namespace: str, rid: str, *, actor="local-demo"):
        with self.transaction():
            self.db.execute(
                "DELETE FROM records WHERE namespace=? AND rid=? AND actor=?",
                (namespace, self.opaque("record", rid), self.opaque("actor", actor)),
            )
            self._append("delete:" + namespace, actor, rid, "ok")

    def purge(self) -> int:
        with self.transaction():
            # Authenticate metadata before trusting the expiry time.
            for row in self.db.execute("SELECT * FROM records"):
                self._row(row)
            count = self.db.execute(
                "DELETE FROM records WHERE expires<=?", (self.clock(),)
            ).rowcount
            self._append("purge", "operator", "", "ok")
        return count

    def rotate(self, cipher: EnvelopeCipher) -> int:
        count = 0
        with self.transaction():
            for row in self.db.execute("SELECT * FROM records").fetchall():
                meta, fields = self._row(row)
                # New cipher must contain old keys during this offline transaction.
                rotated = {
                    name: cipher.rewrap(value, canonical([meta, name]).decode())
                    for name, value in fields.items()
                }
                self.db.execute(
                    "UPDATE records SET fields=?,tag=? WHERE namespace=? AND rid=? AND actor=?",
                    (canonical(rotated).decode(), self.mac(["row-v1", meta, rotated]), *meta[:3]),
                )
                count += 1
            self._append("rotate", "operator", "", "ok")
        self.cipher = cipher
        return count
