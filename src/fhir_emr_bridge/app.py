"""Synthetic-only service with encrypted saved reports. Run one worker."""

from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .crypto import PrivacyError
from .fhir import BridgeError, FhirClient
from .keyfile import load_keys
from .smart import SmartAuth
from .vault import Vault

COOKIE = "bridge_session"


def create_app(settings: Settings | None = None, *, transport=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            base_url=settings.fhir_base_url + "/",
            timeout=httpx.Timeout(30, connect=10),
            verify=not settings.allow_insecure_local_tls,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        ) as http:
            with Vault(settings.privacy_db_path, *load_keys(settings.privacy_key_file)) as vault:
                app.state.http = http
                app.state.vault = vault
                app.state.auth = SmartAuth(settings, http, vault=vault)
                vault.purge()
                try:
                    yield
                finally:
                    for sid in list(app.state.auth.sessions):
                        app.state.auth.logout(sid)

    app = FastAPI(
        title="Synthetic FHIR / OpenEMR Bridge",
        version="0.1.0",
        lifespan=lifespan,
        description="Fixed synthetic data only. See README for HAPI and OpenEMR demos.",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def protect_browser_requests(request: Request, call_next):
        def secure_headers(response):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            if request.url.scheme == "https":
                response.headers["Strict-Transport-Security"] = "max-age=31536000"
            return response

        local = request.client and request.client.host in {"127.0.0.1", "::1", "testclient"}
        tls_required = settings.require_https or urlsplit(settings.redirect_uri).scheme == "https"
        if (
            request.url.scheme != "https"
            and request.url.path != "/health"
            and (tls_required or not local)
        ):
            return secure_headers(JSONResponse({"detail": "HTTPS is required"}, status_code=426))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and origin != str(request.base_url).rstrip("/")
            ):
                return secure_headers(
                    JSONResponse({"detail": "Cross-origin mutation rejected"}, status_code=403)
                )
        audited = request.url.path.startswith(("/demo/", "/openemr/", "/stored/"))
        actor = request.cookies.get(COOKIE, "") if settings.smart_client_id else "local-demo"
        try:
            if audited:
                app.state.vault.audit(
                    "request:" + request.method, actor, request.url.path, "attempt"
                )
            response = await call_next(request)
            if audited:
                app.state.vault.audit(
                    "request:" + request.method, actor, request.url.path, str(response.status_code)
                )
        except PrivacyError:
            response = JSONResponse({"detail": "Privacy safeguard unavailable"}, status_code=503)
        return secure_headers(response)

    @app.exception_handler(PrivacyError)
    async def privacy_error(request, exc):
        return JSONResponse({"detail": "Privacy safeguard unavailable"}, status_code=503)

    @app.exception_handler(BridgeError)
    async def bridge_error(request, exc):
        return JSONResponse(
            {"detail": str(exc), "completed": exc.completed}, status_code=exc.status
        )

    async def fhir(request: Request) -> FhirClient:
        token = (
            await app.state.auth.token(request.cookies.get(COOKIE))
            if (settings.smart_client_id)
            else None
        )
        return FhirClient(app.state.http, token)

    def actor(request: Request) -> str:
        return request.cookies.get(COOKIE, "") if settings.smart_client_id else "local-demo"

    def save(kind: str, record_id: str, data: dict, request: Request) -> dict:
        app.state.vault.put(
            kind, record_id, data, actor=actor(request), ttl=settings.report_retention_seconds
        )
        return data

    @app.get("/health")
    async def health():
        return {"status": "ok", "synthetic_only": True}

    @app.get("/capabilities")
    async def capabilities():
        return await FhirClient(app.state.http).capabilities()

    @app.post("/demo/{run_id}")
    async def write_run(run_id: UUID, request: Request):
        return save("demo", str(run_id), await (await fhir(request)).write_run(run_id), request)

    @app.get("/demo/{run_id}")
    async def read_run(run_id: UUID, request: Request):
        return save("demo", str(run_id), await (await fhir(request)).read_run(run_id), request)

    @app.delete("/demo/{run_id}")
    async def delete_run(run_id: UUID, request: Request):
        result = await (await fhir(request)).delete_run(run_id)
        app.state.vault.delete("demo", str(run_id), actor=actor(request))
        return result

    @app.get("/stored/{kind}/{record_id}")
    async def stored_record(
        kind: Literal["demo", "openemr-summary", "openemr-visits"],
        record_id: UUID,
        request: Request,
    ):
        await fhir(request)
        result = app.state.vault.get(kind, str(record_id), actor=actor(request))
        if result is None:
            raise BridgeError("Saved report not found or expired", status=404)
        return result

    @app.delete("/stored/{kind}/{record_id}")
    async def delete_stored_record(
        kind: Literal["demo", "openemr-summary", "openemr-visits"],
        record_id: UUID,
        request: Request,
    ):
        await fhir(request)
        app.state.vault.delete(kind, str(record_id), actor=actor(request))
        return {"deleted": True}

    @app.get("/auth/login")
    async def login(request: Request):
        app.state.auth.logout(request.cookies.get(COOKIE))
        sid, url = await app.state.auth.begin()
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            COOKIE,
            sid,
            httponly=True,
            samesite="lax",
            max_age=8 * 3600,
            secure=(
                request.url.scheme == "https"
                or settings.require_https
                or urlsplit(settings.redirect_uri).scheme == "https"
            ),
        )
        return response

    @app.get("/auth/callback")
    async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        if error:
            app.state.auth.logout(request.cookies.get(COOKIE))
            raise BridgeError("SMART authorization denied; sign in again", status=400)
        await app.state.auth.complete(request.cookies.get(COOKIE), state, code)
        return {"authorized": True, "next": "/docs", "tokens": "encrypted in server-side vault"}

    @app.post("/auth/logout")
    async def logout(request: Request):
        app.state.auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"authorized": False})
        response.delete_cookie(COOKIE)
        return response

    @app.post("/openemr/demo")
    async def openemr_demo(request: Request):
        if not settings.openemr_seed_enabled:
            raise BridgeError(
                "OpenEMR seeding is disabled; enable only for a disposable demo", status=409
            )
        from .openemr import seed_demo

        return await seed_demo(await fhir(request))

    @app.get("/openemr/patients/{patient_id}")
    async def openemr_patient(patient_id: UUID, request: Request):
        from .openemr import patient_summary

        return save(
            "openemr-summary",
            str(patient_id),
            await patient_summary(await fhir(request), str(patient_id)),
            request,
        )

    @app.get("/openemr/patients/{patient_id}/visits")
    async def openemr_visits(patient_id: UUID, request: Request):
        from .openemr import patient_visits

        return save(
            "openemr-visits",
            str(patient_id),
            await patient_visits(await fhir(request), str(patient_id)),
            request,
        )

    return app


app = create_app()
