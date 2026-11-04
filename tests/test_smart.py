import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from fhir_emr_bridge.config import Settings
from fhir_emr_bridge.fhir import BridgeError
from fhir_emr_bridge.smart import SmartAuth


@pytest.fixture
async def auth_setup():
    clock = [100.0]
    calls = []

    def upstream(request):
        if request.url.path.endswith("smart-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://ehr.test/oauth/authorize",
                    "token_endpoint": "https://ehr.test/oauth/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        params = parse_qs(request.content.decode())
        calls.append(params)
        assert "client_secret" not in params
        return httpx.Response(
            200,
            json={
                "access_token": "private-access-token",
                "token_type": "Bearer",
                "refresh_token": "private-refresh-token",
                "expires_in": 60,
            },
        )

    settings = Settings(fhir_base_url="https://ehr.test/fhir", smart_client_id="public-client")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        auth = SmartAuth(settings, http, clock=lambda: clock[0])
        yield auth, clock, calls


async def test_pkce_code_exchange_keeps_tokens_server_side(auth_setup):
    auth, _, calls = auth_setup
    sid, url = await auth.begin()
    params = parse_qs(urlsplit(url).query)
    assert params["aud"] == ["https://ehr.test/fhir"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["response_type"] == ["code"]
    await auth.complete(sid, params["state"][0], "auth-code")
    verifier = calls[0]["code_verifier"][0]
    assert len(verifier) >= 43
    assert (
        params["code_challenge"][0]
        == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert calls[0]["grant_type"] == ["authorization_code"]
    assert calls[0]["redirect_uri"] == ["http://127.0.0.1:8000/auth/callback"]
    assert await auth.token(sid) == "private-access-token"
    with pytest.raises(BridgeError):
        await auth.complete(sid, params["state"][0], "replayed-code")


async def test_wrong_browser_wrong_state_expired_state_rejected(auth_setup):
    auth, clock, calls = auth_setup
    sid, url = await auth.begin()
    state = parse_qs(urlsplit(url).query)["state"][0]
    for cookie, value in [("another-browser", state), (sid, "wrong-state")]:
        with pytest.raises(BridgeError):
            await auth.complete(cookie, value, "code")
    clock[0] += 601
    with pytest.raises(BridgeError):
        await auth.complete(sid, state, "code")
    assert calls == []


async def test_token_refresh_and_logout(auth_setup):
    auth, clock, calls = auth_setup
    sid, url = await auth.begin()
    state = parse_qs(urlsplit(url).query)["state"][0]
    await auth.complete(sid, state, "code")
    clock[0] += 61
    assert await auth.token(sid) == "private-access-token"
    assert calls[-1]["grant_type"] == ["refresh_token"]
    assert calls[-1]["refresh_token"] == ["private-refresh-token"]
    auth.logout(sid)
    with pytest.raises(BridgeError):
        await auth.token(sid)


async def test_discovery_cannot_send_tokens_to_another_origin():
    settings = Settings(fhir_base_url="https://ehr.test/fhir", smart_client_id="public")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://evil.test/auth",
                    "token_endpoint": "https://evil.test/token",
                },
            )
        )
    ) as http:
        with pytest.raises(BridgeError):
            await SmartAuth(settings, http).begin()


@pytest.mark.parametrize(
    "field,value",
    [
        ("fhir_base_url", "http://remote.test/fhir"),
        ("fhir_base_url", "https://user:password@ehr.test/fhir"),
        ("fhir_base_url", "https://ehr.test/fhir?token=secret"),
        ("allow_insecure_local_tls", True),
        ("redirect_uri", "http://remote.test/callback"),
    ],
)
def test_unsafe_configuration_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


async def test_session_capacity_is_bounded(auth_setup):
    auth, _, _ = auth_setup
    auth.max_sessions = 2
    await auth.begin()
    await auth.begin()
    with pytest.raises(BridgeError) as caught:
        await auth.begin()
    assert caught.value.status == 503


async def test_concurrent_logins_cannot_exceed_session_capacity():
    import asyncio

    gate = asyncio.Event()
    started = [0]

    async def discover(request):
        started[0] += 1
        if started[0] == 2:
            gate.set()
        await gate.wait()
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://ehr.test/auth",
                "token_endpoint": "https://ehr.test/token",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(discover)) as http:
        auth = SmartAuth(
            Settings(fhir_base_url="https://ehr.test/fhir", smart_client_id="demo"), http
        )
        auth.max_sessions = 1
        results = await asyncio.gather(auth.begin(), auth.begin(), return_exceptions=True)
        assert sum(isinstance(result, tuple) for result in results) == 1
        assert sum(isinstance(result, BridgeError) for result in results) == 1


async def test_concurrent_requests_refresh_rotating_token_only_once():
    import asyncio

    clock = [100.0]
    refreshes = [0]

    async def upstream(request):
        if request.url.path.endswith("smart-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://ehr.test/auth",
                    "token_endpoint": "https://ehr.test/token",
                },
            )
        params = parse_qs(request.content.decode())
        if params["grant_type"] == ["refresh_token"]:
            refreshes[0] += 1
            count = refreshes[0]
            await asyncio.sleep(0)
            if count > 1:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-token",
                    "refresh_token": "rotated",
                    "token_type": "Bearer",
                    "expires_in": 60,
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "old-token",
                "refresh_token": "first",
                "token_type": "Bearer",
                "expires_in": 60,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        auth = SmartAuth(
            Settings(fhir_base_url="https://ehr.test/fhir", smart_client_id="demo"),
            http,
            clock=lambda: clock[0],
        )
        sid, url = await auth.begin()
        await auth.complete(sid, parse_qs(urlsplit(url).query)["state"][0], "code")
        clock[0] += 61
        results = await asyncio.gather(auth.token(sid), auth.token(sid), return_exceptions=True)
        assert results == ["fresh-token", "fresh-token"]
        assert refreshes[0] == 1
        assert await auth.token(sid) == "fresh-token"


async def test_confidential_openemr_client_uses_secret_post_and_pkce():
    calls = []

    def upstream(request):
        if request.url.path.endswith("smart-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://ehr.test/auth",
                    "token_endpoint": "https://ehr.test/token",
                },
            )
        params = parse_qs(request.content.decode())
        calls.append(params)
        assert params["client_secret"] == ["generated-demo-secret"]
        if params["grant_type"] == ["authorization_code"]:
            assert len(params["code_verifier"][0]) >= 43
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "token_type": "Bearer",
                "expires_in": 1,
                "refresh_token": "refresh",
            },
        )

    settings = Settings(
        fhir_base_url="https://ehr.test/fhir",
        smart_client_id="demo",
        smart_client_secret="generated-demo-secret",
    )
    assert "generated-demo-secret" not in repr(settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        auth = SmartAuth(settings, http)
        sid, url = await auth.begin()
        assert "generated-demo-secret" not in url
        await auth.complete(sid, parse_qs(urlsplit(url).query)["state"][0], "code")
        assert await auth.token(sid) == "access"
    assert len(calls) == 2
