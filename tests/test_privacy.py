import json
import secrets

import pytest

from fhir_emr_bridge.crypto import EnvelopeCipher, PrivacyError
from fhir_emr_bridge.vault import Vault


def cipher():
    return EnvelopeCipher({"first": secrets.token_bytes(32)}, "first")


def test_envelope_randomized_roundtrip_and_field_binding():
    c = cipher()
    value = {"name": "FICTIONAL", "weight": 72.5}
    a = c.seal(value, "patient/one/name")
    b = c.seal(value, "patient/one/name")
    assert a != b
    assert "FICTIONAL" not in json.dumps(a)
    assert c.open(a, "patient/one/name") == value
    with pytest.raises(PrivacyError):
        c.open(a, "patient/two/name")
    wrong = EnvelopeCipher({"first": secrets.token_bytes(32)}, "first")
    with pytest.raises(PrivacyError):
        wrong.open(a, "patient/one/name")


@pytest.mark.parametrize("field", ["data", "wrapped_key", "nonce", "wrap_nonce", "kid", "v"])
def test_ciphertext_and_envelope_tampering_rejected(field):
    c = cipher()
    envelope = c.seal("token-value", "session/token")
    envelope[field] = 99 if field == "v" else "AAAA"
    with pytest.raises(PrivacyError):
        c.open(envelope, "session/token")


def test_rotation_rewraps_only_data_key_and_old_key_can_be_removed():
    old = secrets.token_bytes(32)
    c = EnvelopeCipher({"old": old}, "old")
    envelope = c.seal("secret-value", "patient/field")
    new = EnvelopeCipher({"old": old, "new": secrets.token_bytes(32)}, "new")
    rotated = new.rewrap(envelope, "patient/field")
    assert rotated["kid"] == "new"
    assert rotated["data"] == envelope["data"]
    assert rotated["nonce"] == envelope["nonce"]
    assert (
        EnvelopeCipher({"new": new.keys["new"]}, "new").open(rotated, "patient/field")
        == "secret-value"
    )
    with pytest.raises(PrivacyError):
        c.open(rotated, "patient/field")


@pytest.mark.parametrize("key", [b"", b"x" * 16, b"x" * 31])
def test_only_256_bit_keys_accepted(key):
    with pytest.raises(ValueError):
        EnvelopeCipher({"key": key}, "key")


def test_persistent_fields_no_plaintext_and_authenticated_reads(tmp_path):
    path = tmp_path / "vault.sqlite3"
    c, audit = cipher(), secrets.token_bytes(32)
    with Vault(path, c, audit) as v:
        v.put(
            "patient", "fictional-id", {"name": "FICTIONAL NAME", "value": 72.5}, actor="session-A"
        )
        assert v.get("patient", "fictional-id", actor="session-A")["name"] == "FICTIONAL NAME"
        checkpoint = v.checkpoint()
    disk = path.read_bytes()
    for plaintext in [b"FICTIONAL NAME", b"fictional-id", b"session-A"]:
        assert plaintext not in disk
    with Vault(path, c, audit) as v:
        assert v.verify(checkpoint)["count"] >= checkpoint["count"]
        with pytest.raises(PrivacyError):
            v.get("patient", "fictional-id", actor="session-B")
        v.delete("patient", "fictional-id", actor="session-A")
        assert v.get("patient", "fictional-id", actor="session-A") is None


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit SET event='{}' WHERE seq=1",
        "DELETE FROM audit WHERE seq=1",
        "UPDATE audit SET mac='forged' WHERE seq=1",
    ],
)
def test_audit_tampering_fails_closed(tmp_path, sql):
    path = tmp_path / "vault.sqlite3"
    c, key = cipher(), secrets.token_bytes(32)
    with Vault(path, c, key) as v:
        v.put("patient", "one", {"name": "FICTIONAL"})
        v.get("patient", "one")
        v.db.execute(sql)
        v.db.commit()
        with pytest.raises(PrivacyError):
            v.verify()
        with pytest.raises(PrivacyError):
            v.get("patient", "one")
        with pytest.raises(PrivacyError):
            v.put("patient", "two", {"name": "OTHER"})


def test_external_checkpoint_detects_suffix_deletion():
    with Vault(":memory:", cipher(), secrets.token_bytes(32)) as v:
        v.put("patient", "one", {"value": 1})
        v.get("patient", "one")
        checkpoint = v.checkpoint()
        v.db.execute("DELETE FROM audit WHERE seq=(SELECT MAX(seq) FROM audit)")
        v.db.commit()
        with pytest.raises(PrivacyError):
            v.verify(checkpoint)


def test_audit_failure_rolls_back_record_write():
    with Vault(":memory:", cipher(), secrets.token_bytes(32)) as v:
        v.db.execute(
            "CREATE TRIGGER broken BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'broken'); END"
        )
        with pytest.raises(PrivacyError):
            v.put("patient", "one", {"value": 1})
        assert v.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


def test_rotation_is_atomic_and_records_read_with_new_key_only(tmp_path):
    old, audit = cipher(), secrets.token_bytes(32)
    path = tmp_path / "vault.sqlite3"
    new_key = secrets.token_bytes(32)
    new = EnvelopeCipher({**old.keys, "second": new_key}, "second")
    with Vault(path, old, audit) as v:
        v.put("patient", "one", {"value": 72.5})
        v.put("token", "sid", {"access_token": "fake-token"})
        assert v.rotate(new) == 2
    with Vault(path, EnvelopeCipher({"second": new_key}, "second"), audit) as v:
        assert v.get("patient", "one") == {"value": 72.5}
        assert v.get("token", "sid") == {"access_token": "fake-token"}
        assert v.verify()["count"] == 5


def test_expiry_prevents_access_and_purge_keeps_audit():
    clock = [100.0]
    with Vault(":memory:", cipher(), secrets.token_bytes(32), clock=lambda: clock[0]) as v:
        v.put("patient", "one", {"value": 1}, ttl=10)
        clock[0] += 11
        assert v.get("patient", "one") is None
        assert v.purge() == 1
        assert v.verify()["count"] == 3


def test_expiry_and_field_swaps_rejected():
    with Vault(":memory:", cipher(), secrets.token_bytes(32)) as v:
        v.put("patient", "one", {"value": 1})
        v.db.execute("UPDATE records SET expires=999999999999")
        v.db.commit()
        with pytest.raises(PrivacyError):
            v.get("patient", "one")


def test_rotation_failure_leaves_every_row_and_audit_unchanged():
    c, audit = cipher(), secrets.token_bytes(32)
    with Vault(":memory:", c, audit) as v:
        v.put("patient", "one", {"value": 1})
        v.cipher = EnvelopeCipher({"unavailable": secrets.token_bytes(32)}, "unavailable")
        v.put("patient", "two", {"value": 2})
        before = v.db.execute("SELECT * FROM records ORDER BY rowid").fetchall()
        checkpoint = v.checkpoint()
        with pytest.raises(PrivacyError):
            v.rotate(EnvelopeCipher({**c.keys, "new": secrets.token_bytes(32)}, "new"))
        assert v.db.execute("SELECT * FROM records ORDER BY rowid").fetchall() == before
        assert v.checkpoint() == checkpoint


def test_concurrent_access_keeps_one_valid_audit_sequence():
    from concurrent.futures import ThreadPoolExecutor

    with Vault(":memory:", cipher(), secrets.token_bytes(32)) as v:
        v.put("patient", "one", {"value": 72.5})
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: v.get("patient", "one"), range(20)))
        assert results == [{"value": 72.5}] * 20
        assert v.verify()["count"] == 21


def test_changed_fields_and_wrong_database_key_rejected(tmp_path):
    path, audit, c = tmp_path / "store.sqlite3", secrets.token_bytes(32), cipher()
    with Vault(path, c, audit) as v:
        v.put("patient", "one", {"value": 72.5, "name": "FICTIONAL"})
    with Vault(path, cipher(), audit) as v:
        with pytest.raises(PrivacyError):
            v.get("patient", "one")
    with Vault(path, c, audit) as v:
        fields = json.loads(v.db.execute("SELECT fields FROM records").fetchone()[0])
        del fields["name"]
        v.db.execute("UPDATE records SET fields=?", (json.dumps(fields),))
        v.db.commit()
        with pytest.raises(PrivacyError):
            v.get("patient", "one")
