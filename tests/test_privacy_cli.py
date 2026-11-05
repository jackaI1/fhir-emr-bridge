import json
import subprocess
import sys

import pytest

from fhir_emr_bridge.crypto import PrivacyError
from fhir_emr_bridge.keyfile import create_keys, load_keys
from fhir_emr_bridge.vault import Vault


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "fhir_emr_bridge.privacy_cli", *map(str, args)],
        capture_output=True,
        text=True,
    )


def test_operator_workflow_rotation_checkpoint_and_purge(tmp_path):
    keyfile, db, checkpoint = (
        tmp_path / "keys.json",
        tmp_path / "store.sqlite3",
        tmp_path / "head.json",
    )
    assert cli("init", "--keys", keyfile).returncode == 0
    assert keyfile.stat().st_mode & 0o077 == 0
    original = keyfile.read_bytes()
    assert cli("init", "--keys", keyfile).returncode != 0
    assert keyfile.read_bytes() == original
    with Vault(db, *load_keys(keyfile)) as v:
        v.put("patient", "one", {"name": "FICTIONAL"})
    result = cli("checkpoint", "--keys", keyfile, "--db", db, "--output", checkpoint)
    assert result.returncode == 0, result.stderr
    assert cli("add-key", "--keys", keyfile, "--id", "v2").returncode == 0
    assert json.loads(keyfile.read_text())["active"] == "v2"
    assert cli("rotate", "--keys", keyfile, "--db", db).returncode == 0
    data = json.loads(keyfile.read_text())
    del data["keys"]["v1"]
    keyfile.write_text(json.dumps(data))
    with Vault(db, *load_keys(keyfile)) as v:
        assert v.get("patient", "one")["name"] == "FICTIONAL"
    assert cli("verify", "--keys", keyfile, "--db", db, "--checkpoint", checkpoint).returncode == 0
    assert cli("purge", "--keys", keyfile, "--db", db).returncode == 0
    with Vault(db, *load_keys(keyfile)) as v:
        v.db.execute("UPDATE audit SET mac='changed' WHERE seq=1")
        v.db.commit()
    result = cli("verify", "--keys", keyfile, "--db", db)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "FICTIONAL" not in result.stderr


def test_key_permissions_and_bad_format_fail_closed(tmp_path):
    path = tmp_path / "keys.json"
    create_keys(path)
    path.chmod(0o644)
    with pytest.raises(PrivacyError):
        load_keys(path)
    path.chmod(0o600)
    path.write_text('{"keys": "malformed"}')
    with pytest.raises(PrivacyError):
        load_keys(path)


def test_offline_smoke_is_repeatable_and_cleans_up():
    result = subprocess.run(
        [sys.executable, "scripts/smoke_privacy.py"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["encrypted_fields"] == 7
    assert report["rotation_verified"] is True
    assert report["tampering_detected"] is True
    assert report["temporary_files_removed"] is True
