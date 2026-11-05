import json
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_app import RUN
from test_fhir import FhirServer

from fhir_emr_bridge.app import create_app
from fhir_emr_bridge.config import Settings
from fhir_emr_bridge.crypto import encode


def persistent_settings(tmp_path, **kwargs):
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "active": "v1",
                "keys": {"v1": encode(secrets.token_bytes(32))},
                "audit": encode(secrets.token_bytes(32)),
            }
        )
    )
    path.chmod(0o600)
    return Settings(
        privacy_db_path=str(tmp_path / "vault.sqlite3"),
        privacy_key_file=path,
        fhir_base_url="https://fhir.test/fhir",
        **kwargs,
    )


def test_actual_routes_store_read_and_delete_encrypted_snapshot(tmp_path):
    settings = persistent_settings(tmp_path)
    app = create_app(settings, transport=httpx.MockTransport(FhirServer()))
    with TestClient(app) as client:
        original = client.post(f"/demo/{RUN}")
        assert original.status_code == 200
        saved = client.get(f"/stored/demo/{RUN}")
        assert saved.status_code == 200
        assert saved.json() == original.json()
        assert client.delete(f"/demo/{RUN}").status_code == 200
        assert client.get(f"/stored/demo/{RUN}").status_code == 404
        assert app.state.vault.verify()["count"] >= 10
    disk = (tmp_path / "vault.sqlite3").read_bytes()
    assert b"72.5" not in disk
    assert str(RUN).encode() not in disk


def test_snapshots_survive_restart_with_operator_keys(tmp_path):
    settings = persistent_settings(tmp_path)
    with TestClient(create_app(settings, transport=httpx.MockTransport(FhirServer()))) as client:
        assert client.post(f"/demo/{RUN}").status_code == 200
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/stored/demo/{RUN}").json()["verified"] is True
        assert client.delete(f"/stored/demo/{RUN}").status_code == 200
        assert client.get(f"/stored/demo/{RUN}").status_code == 404


def test_broken_audit_blocks_upstream_record_access():
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(500)

    app = create_app(Settings(), transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        app.state.vault.audit("setup")
        app.state.vault.db.execute("UPDATE audit SET mac='tampered'")
        app.state.vault.db.commit()
        response = client.get(f"/demo/{RUN}")
        assert response.status_code == 503
        assert calls == []
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers", [{}, {"X-Forwarded-Proto": "https"}, {"Host": "localhost"}])
def test_remote_http_is_rejected_before_upstream(headers):
    calls = []
    app = create_app(Settings(), transport=httpx.MockTransport(lambda r: calls.append(r)))
    with TestClient(app, client=("198.51.100.1", 5000)) as client:
        response = client.get(f"/demo/{RUN}", headers=headers)
        assert response.status_code == 426
        assert calls == []


def test_https_callback_mode_requires_tls_even_for_local_requests():
    app = create_app(Settings(redirect_uri="https://localhost/auth/callback"))
    with TestClient(app) as client:
        assert client.get("/auth/login").status_code == 426
    with TestClient(app, base_url="https://localhost") as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert "max-age=" in response.headers["strict-transport-security"]


def test_disk_storage_requires_explicit_keys(tmp_path):
    with pytest.raises(ValueError):
        Settings(privacy_db_path=str(tmp_path / "vault.sqlite3"))


def test_real_smart_flow_stores_ciphertext_refreshes_and_erases_tokens(tmp_path):
    tokens = [
        "fake-access-first",
        "fake-refresh-first",
        "fake-access-second",
        "fake-refresh-second",
    ]
    count = [0]
    fhir = FhirServer()

    def upstream(request):
        if request.url.path.endswith("smart-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://fhir.test/auth",
                    "token_endpoint": "https://fhir.test/token",
                },
            )
        if request.url.path == "/token":
            offset = count[0] * 2
            count[0] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": tokens[offset],
                    "refresh_token": tokens[offset + 1],
                    "token_type": "Bearer",
                    "expires_in": 1 if offset == 0 else 300,
                },
            )
        if request.url.path.endswith("/metadata"):
            return fhir(request)
        assert request.headers["Authorization"] == "Bearer fake-access-second"
        return fhir(request)

    app = create_app(
        persistent_settings(tmp_path, smart_client_id="demo"),
        transport=httpx.MockTransport(upstream),
    )
    with TestClient(app, follow_redirects=False) as client:
        login = client.get("/auth/login")
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        assert (
            client.get("/auth/callback", params={"state": state, "code": "code"}).status_code == 200
        )
        for token in tokens:
            assert token not in repr(app.state.auth.sessions)
        assert client.post(f"/demo/{RUN}").status_code == 200
        assert client.get(f"/stored/demo/{RUN}").status_code == 200
        sid = client.cookies.get("bridge_session")
        client.cookies.clear()
        assert client.get(f"/stored/demo/{RUN}").status_code == 401
        client.cookies.set("bridge_session", sid)
        assert client.post("/auth/logout").status_code == 200
        assert (
            app.state.vault.db.execute(
                "SELECT COUNT(*) FROM records WHERE namespace='tokens'"
            ).fetchone()[0]
            == 0
        )
    disk = (tmp_path / "vault.sqlite3").read_bytes()
    assert all(token.encode() not in disk for token in tokens)


def test_https_login_always_sets_secure_cookie():
    def discovery(request):
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://fhir.test/auth",
                "token_endpoint": "https://fhir.test/token",
            },
        )

    app = create_app(
        Settings(
            require_https=True, smart_client_id="demo", fhir_base_url="https://fhir.test/fhir"
        ),
        transport=httpx.MockTransport(discovery),
    )
    with TestClient(app, base_url="https://localhost", follow_redirects=False) as client:
        response = client.get("/auth/login")
        assert response.status_code == 302
        assert "Secure" in response.headers["set-cookie"]
