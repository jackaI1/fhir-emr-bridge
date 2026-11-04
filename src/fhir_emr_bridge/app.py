"""Small synthetic-only service. Run one worker: SMART sessions live in memory."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .fhir import BridgeError, FhirClient
from .smart import SmartAuth

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
            app.state.http = http
            app.state.auth = SmartAuth(settings, http)
            yield

    app = FastAPI(
        title="Synthetic FHIR / OpenEMR Bridge",
        version="0.1.0",
        lifespan=lifespan,
        description="Fixed synthetic data only. See README for HAPI and OpenEMR demos.",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def protect_browser_requests(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and origin != str(request.base_url).rstrip("/")
            ):
                return JSONResponse({"detail": "Cross-origin mutation rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

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

    @app.get("/health")
    async def health():
        return {"status": "ok", "synthetic_only": True}

    @app.get("/capabilities")
    async def capabilities():
        return await FhirClient(app.state.http).capabilities()

    @app.post("/demo/{run_id}")
    async def write_run(run_id: UUID, request: Request):
        return await (await fhir(request)).write_run(run_id)

    @app.get("/demo/{run_id}")
    async def read_run(run_id: UUID, request: Request):
        return await (await fhir(request)).read_run(run_id)

    @app.delete("/demo/{run_id}")
    async def delete_run(run_id: UUID, request: Request):
        return await (await fhir(request)).delete_run(run_id)

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
            secure=urlsplit(settings.redirect_uri).scheme == "https",
        )
        return response

    @app.get("/auth/callback")
    async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        if error:
            app.state.auth.logout(request.cookies.get(COOKIE))
            raise BridgeError("SMART authorization denied; sign in again", status=400)
        await app.state.auth.complete(request.cookies.get(COOKIE), state, code)
        return {"authorized": True, "next": "/docs", "tokens": "stored only in server memory"}

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

        return await patient_summary(await fhir(request), str(patient_id))

    @app.get("/openemr/patients/{patient_id}/visits")
    async def openemr_visits(patient_id: UUID, request: Request):
        from .openemr import patient_visits

        return await patient_visits(await fhir(request), str(patient_id))

    return app


app = create_app()
