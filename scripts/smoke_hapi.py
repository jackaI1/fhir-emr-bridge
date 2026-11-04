"""Real public-server round trip through the service, with finally cleanup.

Run: uv run python scripts/smoke_hapi.py
No listener, background process, or browser is created.
"""

import asyncio
import json
from uuid import uuid4

import httpx

from fhir_emr_bridge.app import create_app
from fhir_emr_bridge.config import Settings


async def main():
    app = create_app(
        Settings(
            fhir_base_url="https://hapi.fhir.org/baseR4",
            smart_client_id="",
            allow_insecure_local_tls=False,
            openemr_seed_enabled=False,
        )
    )
    run_id = uuid4()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8000", timeout=180
        ) as client,
    ):
        try:
            for _ in range(2):
                response = await client.post(f"/demo/{run_id}")
                assert response.is_success, response.text
                report = response.json()
            response = await client.get(f"/demo/{run_id}")
            assert response.is_success, response.text
            assert response.json()["verified"] is True
            visits = response.json()["visit_report"]
            assert visits["counts"] == {
                "total": 3,
                "assigned": 3,
                "unassigned": 0,
                "excluded": 0,
            }, visits
            assert len(visits["visits"]) == 1, visits
            print(
                json.dumps(
                    {"upstream": "https://hapi.fhir.org/baseR4", **report, "repeat_writes": 2},
                    indent=2,
                )
            )
        finally:
            cleanup = await client.delete(f"/demo/{run_id}")
            assert cleanup.is_success, cleanup.text
            print(json.dumps({"cleanup": cleanup.json()}))


if __name__ == "__main__":
    asyncio.run(main())
