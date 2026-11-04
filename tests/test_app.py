from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from test_fhir import FhirServer

from fhir_emr_bridge.app import create_app
from fhir_emr_bridge.config import Settings

RUN = UUID("ad0eb8de-b9ad-4ab8-9735-316f612a0183")


def test_http_round_trip_and_validation():
    app = create_app(
        Settings(fhir_base_url="https://fhir.test/fhir"),
        transport=httpx.MockTransport(FhirServer()),
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/health").json() == {"status": "ok", "synthetic_only": True}
        assert client.post(f"/demo/{RUN}").json()["resource_count"] == 5
        assert client.get(f"/demo/{RUN}").json()["verified"] is True
        assert client.delete(f"/demo/{RUN}").json()["deleted"] == 5
        assert client.post("/demo/not-a-uuid").status_code == 422
        assert (
            client.post(f"/demo/{RUN}", headers={"Origin": "https://evil.test"}).status_code == 403
        )
        assert (
            client.post(f"/demo/{RUN}", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        )
        assert client.get("/health", headers={"Host": "evil.test"}).status_code == 400


def test_errors_do_not_relay_upstream_bodies():
    app = create_app(
        Settings(fhir_base_url="https://fhir.test/fhir"),
        transport=httpx.MockTransport(lambda _: httpx.Response(500, text="secret")),
    )
    with TestClient(app) as client:
        response = client.get("/capabilities")
        assert response.status_code == 502
        assert "secret" not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_smart_browser_flow_never_returns_tokens():
    fhir = FhirServer()

    def upstream(request):
        if request.url.path.endswith("smart-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://fhir.test/authorize",
                    "token_endpoint": "https://fhir.test/token",
                },
            )
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "SECRET_TOKEN",
                    "token_type": "Bearer",
                    "expires_in": 300,
                },
            )
        if request.url.path.endswith("/metadata"):
            return fhir(request)
        assert request.headers["Authorization"] == "Bearer SECRET_TOKEN"
        return fhir(request)

    app = create_app(
        Settings(fhir_base_url="https://fhir.test/fhir", smart_client_id="demo"),
        transport=httpx.MockTransport(upstream),
    )
    with TestClient(app, base_url="http://127.0.0.1:8000", follow_redirects=False) as client:
        assert client.post(f"/demo/{RUN}").status_code == 401
        login = client.get("/auth/login")
        assert login.status_code == 302
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=lax" in login.headers["set-cookie"]
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        callback = client.get("/auth/callback", params={"state": state, "code": "demo-code"})
        assert callback.status_code == 200
        assert "SECRET_TOKEN" not in callback.text
        assert client.post(f"/demo/{RUN}").json()["verified"] is True
        assert (
            client.get("/auth/callback", params={"state": state, "code": "replay"}).status_code
            == 400
        )
        assert client.post("/auth/logout").status_code == 200
        assert client.get(f"/demo/{RUN}").status_code == 401


def test_seed_disabled_by_default():
    with TestClient(create_app(Settings())) as client:
        assert client.post("/openemr/demo").status_code == 409


@pytest.mark.parametrize(
    "token_response",
    [
        {"access_token": "secret", "token_type": "MAC"},
        {"access_token": "secret", "token_type": None},
        {"access_token": "secret", "token_type": "Bearer", "expires_in": -1},
        {"access_token": 123, "token_type": "Bearer"},
    ],
)
def test_malformed_token_is_rejected(token_response):
    def upstream(request):
        if request.url.path.endswith("smart-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://fhir.test/auth",
                    "token_endpoint": "https://fhir.test/token",
                },
            )
        return httpx.Response(200, json=token_response)

    app = create_app(
        Settings(fhir_base_url="https://fhir.test/fhir", smart_client_id="demo"),
        transport=httpx.MockTransport(upstream),
    )
    with TestClient(app, follow_redirects=False) as client:
        login = client.get("/auth/login")
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        result = client.get("/auth/callback", params={"state": state, "code": "code"})
        assert result.status_code == 502
        assert "secret" not in result.text
