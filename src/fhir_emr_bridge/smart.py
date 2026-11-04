"""Standalone SMART authorization code flow for a registered public or confidential client.

Access/refresh tokens stay in bounded, expiring server memory. An opaque cookie
binds PKCE and OAuth state to the initiating browser; no identity claims are used.
"""

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx

from .config import Settings, safe_url
from .fhir import BridgeError, request_json


def origin(url: str) -> tuple:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


@dataclass
class Session:
    state: str
    verifier: str
    token_endpoint: str
    deadline: float
    access_token: str = ""
    refresh_token: str = ""
    token_expiry: float = 0
    pending: bool = True
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SmartAuth:
    max_sessions = 128

    def __init__(self, settings: Settings, http: httpx.AsyncClient, clock=time.monotonic):
        self.settings, self.http, self.clock = settings, http, clock
        self.sessions: dict[str, Session] = {}

    def client_credentials(self) -> dict:
        credentials = {"client_id": self.settings.smart_client_id}
        secret = self.settings.smart_client_secret.get_secret_value()
        if secret:
            credentials["client_secret"] = secret
        return credentials

    def prune(self):
        now = self.clock()
        self.sessions = {sid: s for sid, s in self.sessions.items() if s.deadline > now}

    async def begin(self) -> tuple[str, str]:
        if not self.settings.smart_client_id:
            raise BridgeError("Configure a registered SMART client first", status=409)
        self.prune()
        if len(self.sessions) >= self.max_sessions:
            raise BridgeError("Session capacity reached; try again later", status=503)
        config = await request_json(
            self.http, "GET", self.settings.fhir_base_url + "/.well-known/smart-configuration"
        )
        endpoints = []
        for key in ["authorization_endpoint", "token_endpoint"]:
            try:
                endpoint = safe_url(config[key])
                if origin(endpoint) != origin(self.settings.fhir_base_url):
                    raise ValueError("Cross-origin authorization endpoint")
            except (KeyError, ValueError, TypeError) as exc:
                raise BridgeError("Invalid or cross-origin SMART discovery endpoint") from exc
            endpoints.append(endpoint)
        if "code_challenge_methods_supported" in config and (
            "S256" not in config["code_challenge_methods_supported"]
        ):
            raise BridgeError("SMART server does not support S256 PKCE")
        sid, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        # Discovery yields: recheck atomically before inserting the session.
        self.prune()
        if len(self.sessions) >= self.max_sessions:
            raise BridgeError("Session capacity reached; try again later", status=503)
        self.sessions[sid] = Session(state, verifier, endpoints[1], self.clock() + 600)
        params = {
            "response_type": "code",
            "client_id": self.settings.smart_client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": self.settings.smart_scopes,
            "state": state,
            "aud": self.settings.fhir_base_url,
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
        }
        return sid, endpoints[0] + "?" + urlencode(params)

    def session(self, sid: str | None) -> Session:
        self.prune()
        if not sid or sid not in self.sessions:
            raise BridgeError("SMART login required or session expired", status=401)
        return self.sessions[sid]

    def save_tokens(self, session: Session, data: dict):
        access_token = data.get("access_token")
        expiry = data.get("expires_in", 300)
        token_type = data.get("token_type")
        refresh = data.get("refresh_token", session.refresh_token)
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or not 0 < expiry <= 86400 * 365
            or not isinstance(refresh, str)
        ):
            raise BridgeError("Invalid SMART token response")
        session.access_token, session.refresh_token = access_token, refresh
        session.token_expiry = self.clock() + expiry
        session.deadline = self.clock() + 8 * 3600

    async def complete(self, sid: str | None, state: str, code: str):
        try:
            session = self.session(sid)
        except BridgeError as exc:
            raise BridgeError("Invalid or expired authorization state", status=400) from exc
        if not session.pending or not secrets.compare_digest(session.state, state) or not code:
            raise BridgeError("Invalid or replayed authorization state", status=400)
        session.pending = False  # consume BEFORE awaiting the one-use token exchange
        try:
            data = await request_json(
                self.http,
                "POST",
                session.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    **self.client_credentials(),
                    "redirect_uri": self.settings.redirect_uri,
                    "code": code,
                    "code_verifier": session.verifier,
                },
            )
            self.save_tokens(session, data)
        except BridgeError:
            self.logout(sid)
            raise
        finally:
            session.verifier = ""
            session.state = ""

    async def token(self, sid: str | None) -> str:
        session = self.session(sid)
        if session.pending or not session.access_token:
            raise BridgeError("Complete SMART login first", status=401)
        async with session.refresh_lock:
            # Another request may already have rotated this refresh token.
            self.session(sid)
            if session.token_expiry <= self.clock() + 10:
                if not session.refresh_token:
                    self.logout(sid)
                    raise BridgeError("SMART token expired; sign in again", status=401)
                try:
                    data = await request_json(
                        self.http,
                        "POST",
                        session.token_endpoint,
                        data={
                            "grant_type": "refresh_token",
                            **self.client_credentials(),
                            "refresh_token": session.refresh_token,
                        },
                    )
                    self.save_tokens(session, data)
                except BridgeError:
                    self.logout(sid)
                    raise
        return session.access_token

    def logout(self, sid: str | None):
        self.sessions.pop(sid, None)
