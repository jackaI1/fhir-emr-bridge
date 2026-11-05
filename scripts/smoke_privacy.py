"""Offline privacy walkthrough with the real bridge and a synthetic FHIR HTTP sandbox.

No listeners, external requests, browser pages or lasting files are created.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from fhir_emr_bridge.app import create_app
from fhir_emr_bridge.config import Settings
from fhir_emr_bridge.crypto import PrivacyError
from fhir_emr_bridge.keyfile import create_keys, load_keys
from fhir_emr_bridge.privacy_cli import add_key
from fhir_emr_bridge.synthetic import resources
from fhir_emr_bridge.vault import Vault


def main():
    run_id = uuid4()
    records = {r["resourceType"] + "/" + r["id"]: r for r in resources(run_id)}

    def sandbox(request):
        record = records.get(request.url.path.removeprefix("/fhir/"))
        return httpx.Response(200, json=record) if record else httpx.Response(404)

    with TemporaryDirectory(prefix="bridge-privacy-") as directory:
        folder = Path(directory)
        keys, db = folder / "keys.json", folder / "vault.sqlite3"
        create_keys(keys)
        settings = Settings(
            privacy_db_path=str(db),
            privacy_key_file=keys,
            fhir_base_url="https://sandbox.invalid/fhir",
        )
        app = create_app(settings, transport=httpx.MockTransport(sandbox))
        with TestClient(app) as client:
            original = client.get(f"/demo/{run_id}")
            assert original.status_code == 200
            assert client.get(f"/stored/demo/{run_id}").json() == original.json()
            fields = json.loads(
                app.state.vault.db.execute("SELECT fields FROM records").fetchone()[0]
            )
            encrypted_fields = len(fields)
            checkpoint = app.state.vault.checkpoint()
        assert b"72.5" not in db.read_bytes()
        assert str(run_id).encode() not in db.read_bytes()
        add_key(keys, "v2")
        with Vault(db, *load_keys(keys)) as vault:
            assert vault.rotate(vault.cipher) == 1
            assert vault.get("demo", str(run_id)) == original.json()
            vault.verify(checkpoint)
            audit_count = vault.verify()["count"]
            vault.db.execute("UPDATE audit SET mac='tampered' WHERE seq=1")
            vault.db.commit()
            try:
                vault.verify()
            except PrivacyError:
                tampering_detected = True
            else:
                raise AssertionError("Tampering was not detected")
    assert not folder.exists()
    print(
        json.dumps(
            {
                "sandbox": "offline synthetic FHIR transport",
                "encrypted_fields": encrypted_fields,
                "audit_events": audit_count,
                "rotation_verified": True,
                "tampering_detected": tampering_detected,
                "temporary_files_removed": True,
            }
        )
    )


if __name__ == "__main__":
    main()
