# fhir-emr-bridge

A synthetic clinical integration service by Jack Lai, University of Waterloo Nanotechnology Engineering. It checks whether an EHR exposes the FHIR operations an integration needs, exercises a repeatable patient/vitals workflow, and verifies the returned data and references.

- **Public HAPI FHIR R4:** write, read, verify, repeat and clean up one Patient, one Encounter and three Observations.
- **Local OpenEMR:** complete real SMART authorization code + S256 PKCE login/consent, seed fictional records through OpenEMR's standard API, then read Patient, Encounter and Observation resources through its FHIR R4 API.
- **Synthetic data only:** the API accepts a run UUID, never clinical payloads, names or uploaded records.

OpenEMR 7.0.4's FHIR endpoints for these resources are read/search endpoints. This project deliberately reports unsupported FHIR writes instead of pretending they work; HAPI supplies the FHIR write demonstration. The OpenEMR seed path uses its **standard REST API**, with the same SMART bearer token, and requires explicit localhost-only opt-in.

## Try the real public-server round trip

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/jackaI1/fhir-emr-bridge.git
cd fhir-emr-bridge
uv sync --frozen
uv run python scripts/smoke_hapi.py
```

This calls the actual [HAPI R4 public test server](https://hapi.fhir.org/), through the service in process. It writes the same five resources twice, checks their values and links, reads them again, and deletes them in a `finally` block. It opens no ports or browser pages. Success includes `"verified": true`, `"resource_count": 5`, `"repeat_writes": 2` and `"deleted": 5`. HAPI is shared, unauthenticated, periodically purged and may be unavailable; never send it private information. A failed run prints its UUID so cleanup can be retried through the API.

## Run the HTTP service

```sh
uv run uvicorn fhir_emr_bridge.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open [interactive API documentation](http://127.0.0.1:8000/docs). In another terminal:

```sh
RUN_ID=$(uv run python -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body -X POST "http://127.0.0.1:8000/demo/$RUN_ID"
curl --fail-with-body "http://127.0.0.1:8000/demo/$RUN_ID"
curl --fail-with-body -X DELETE "http://127.0.0.1:8000/demo/$RUN_ID"
```

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Process health; does not imply upstream availability |
| `GET /capabilities` | Check upstream FHIR version and advertised capabilities |
| `POST /demo/{uuid}` | Upsert and verify the fixed five-resource synthetic run |
| `GET /demo/{uuid}` | Read and verify the same run |
| `DELETE /demo/{uuid}` | Check ownership of every resource, then delete observations before encounter and patient |
| `GET /auth/login`, `GET /auth/callback` | SMART standalone browser authorization |
| `POST /auth/logout` | Discard the local session and tokens |
| `POST /openemr/demo` | Opt-in local native-API seed followed by FHIR reads |
| `GET /openemr/patients/{uuid}` | Summarize only the fixed synthetic OpenEMR patient |

IDs derive from the run UUID. Use a fresh UUID per independent run. Writes use instance PUT when `updateCreate` is advertised, or conditional PUT with `_id` when `conditionalUpdate` is advertised and `updateCreate` is omitted. An omitted field means unknown update-as-create support: this path is verified against HAPI, but another server may reject it. Explicit `updateCreate: false` is refused before writing. Requests use `application/fhir+json`. Verification requires all expected clinical fields, synthetic tags, LOINC codes, UCUM quantities and patient/encounter references; server-added metadata is permitted.

The five writes are sequential, **not an atomic FHIR transaction**. An error reports `completed`; repeat the same UUID to recover or use DELETE to clean up. Existing resources without the project tag are never overwritten or deleted. Public HAPI deletion is a logical FHIR deletion, not guaranteed erasure of server history.

## Docker service

```sh
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/health
docker compose down --remove-orphans
```

The container binds only `127.0.0.1:8000` and needs no credentials for HAPI. No cloud hosting or paid account is required; the public source and actual public-server integration are reproducible by anyone.

## Docker Compose OpenEMR demo

Requires Docker Engine/Desktop with Compose v2+, about 2 GB available container memory, and ports 9300 and (for an optional interactive bridge) 8000 free. On macOS, Docker Desktop or a separately managed Colima VM works. The demo uses `openemr/openemr:7.0.4` and MariaDB 11.4, has memory/CPU limits, and never exposes its database port.

```sh
uv run python scripts/demo_env.py
docker compose -f compose.openemr.yaml up -d --wait --wait-timeout 600
uv run python scripts/smoke_openemr.py
```

`demo_env.py` generates unique local credentials in ignored `.env` with mode 600; it refuses to overwrite an existing file. Setup can take several minutes. `smoke_openemr.py` explicitly configures API globals in **this disposable instance**, dynamically registers and enables a confidential OAuth client, and drives the actual OpenEMR login and consent forms over HTTPS. It exchanges the resulting code using the service's PKCE verifier, seeds one obviously fictional patient/encounter/vitals form, and reads them through authenticated FHIR searches. It checks vital-sign codes, reads the summary again and confirms unsupported FHIR writes are rejected. No password grant, fabricated access token or skipped consent is used. Each smoke run creates a new fictional patient.

The smoke uses the in-process service, so no callback listener or browser is required. Its client ID is saved in ignored `.local/openemr-client-id`; the generated secret is stored separately in ignored `.local/openemr-client-secret` with mode 600. OpenEMR requires confidential clients for `user/` scopes; code exchange and refresh use `client_secret_post` in addition to PKCE. Other compatible servers may use a public client by leaving the secret unset. For an interactive session afterward:

```sh
export BRIDGE_FHIR_BASE_URL=https://localhost:9300/apis/default/fhir
export BRIDGE_SMART_CLIENT_ID="$(cat .local/openemr-client-id)"
export BRIDGE_SMART_CLIENT_SECRET="$(cat .local/openemr-client-secret)"
export BRIDGE_SMART_SCOPES='openid offline_access api:fhir user/Patient.read user/Encounter.read user/Observation.read api:oemr user/patient.read user/patient.write user/encounter.read user/encounter.write user/vital.read user/vital.write'
export BRIDGE_ALLOW_INSECURE_LOCAL_TLS=true
export BRIDGE_OPENEMR_SEED_ENABLED=true
uv run uvicorn fhir_emr_bridge.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Visit `http://127.0.0.1:8000/auth/login` using that exact hostname, log in as `admin` with the generated `DEMO_ADMIN_PASSWORD` from your local `.env`, approve access, then open `/docs` and call `POST /openemr/demo`. Reuse its returned patient UUID with `GET /openemr/patients/{uuid}`. Sessions require one worker and expire after eight hours; restarting the service requires login again.

Stop the bridge with Ctrl-C and destroy **only this demo's** containers and synthetic database when finished:

```sh
docker compose -f compose.openemr.yaml down -v --remove-orphans
```

The named volumes preserve synthetic records until that command. Delete your local `.env` and `.local/openemr-client-id` / `.local/openemr-client-secret` when no longer needed. If using a dedicated Colima profile, stop that profile too. The repository does not commit passwords, tokens, private keys or patient records.

## Configuration and security boundaries

Settings use the `BRIDGE_` environment prefix (or ignored `.env`); see [`config.py`](src/fhir_emr_bridge/config.py). Default upstream: `https://hapi.fhir.org/baseR4`. `BRIDGE_SMART_CLIENT_SECRET` optionally supplies confidential-client authentication and is masked in settings representations. A configured `BRIDGE_SMART_CLIENT_ID` requires a valid browser session before data operations. Additional settings include `BRIDGE_REDIRECT_URI` and JSON-array `BRIDGE_ALLOWED_HOSTS`. HTTP is allowed only on loopback; disabling TLS verification is allowed only for a loopback upstream and is used for OpenEMR's generated demo certificate.

SMART discovery is restricted to the configured upstream origin. State is random, single-use, browser-bound and valid for ten minutes. Tokens remain in bounded server memory, refresh before expiration, and never appear in API responses. Cookies are HttpOnly and SameSite=Lax (Secure for HTTPS callbacks). This is delegated authorization; the service does not consume ID-token identity claims. Logout removes local credentials; it does not revoke the upstream grant. Request origins and hostnames are checked, redirects are not followed by the FHIR client, pagination is bounded to the configured FHIR base, and upstream errors are sanitized. Keep access logging disabled so callback codes are not logged.

This is a development integration demonstrator, not a clinical system. Use one worker on localhost. Internet hosting would require operator authentication/rate limiting, persistent encrypted sessions, HTTPS termination and deployment-specific controls. Do not point it at a real patient database. It does not claim full SMART certification, EHR-launch support, US Core validation or clinical decision support.

## Tests and CI

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

The repository initially contained only a README and had no existing tests. The suite covers transport errors, incompatible capabilities, partial writes/retry, linked-resource verification, safe cleanup, SMART PKCE/state/replay/expiry/refresh/logout, synthetic-only reads, native API response shapes and safe pagination. HTTP boundary fixtures keep the default suite deterministic and offline. The separate HAPI smoke is opt-in and requires internet access.

[GitHub Actions](https://github.com/jackaI1/fhir-emr-bridge/actions) runs the full suite on Python 3.12 and 3.13, lint/format checks, a built-container health check, and the actual Docker OpenEMR SMART/FHIR smoke. Disposable CI containers and volumes are removed in `always()` steps. See [`docs/verification.md`](docs/verification.md) for measured live results.

## Skills demonstrated

| Skill | What it does | File paths |
| --- | --- | --- |
| **OpenEMR / FHIR integration** | Authenticates to OpenEMR using SMART authorization code + S256 PKCE; seeds a synthetic clinical visit via its standard API and reads Patient, Encounter and Observation FHIR R4 resources | [`smart.py`](src/fhir_emr_bridge/smart.py), [`openemr.py`](src/fhir_emr_bridge/openemr.py), [`compose.openemr.yaml`](compose.openemr.yaml), [`smoke_openemr.py`](scripts/smoke_openemr.py) |
| **FHIR R4 read/write workflows** | Repeats and verifies five linked synthetic resources on public HAPI, with capability-driven upserts, LOINC/UCUM coding, partial-failure reporting and scoped cleanup | [`fhir.py`](src/fhir_emr_bridge/fhir.py), [`synthetic.py`](src/fhir_emr_bridge/synthetic.py), [`smoke_hapi.py`](scripts/smoke_hapi.py) |
| **API engineering and automated verification** | Serves an OpenAPI HTTP interface, protects OAuth sessions, tests failure cases and runs real container integration checks in CI | [`app.py`](src/fhir_emr_bridge/app.py), [`tests/`](tests), [`ci.yml`](.github/workflows/ci.yml), [`Dockerfile`](Dockerfile) |

## Protocol references

- [HL7 FHIR R4 REST interactions](https://hl7.org/fhir/R4/http.html) and [SMART App Launch authorization](https://hl7.org/fhir/smart-app-launch/app-launch.html).
- [OpenEMR 7.0.4 authentication](https://github.com/openemr/openemr/blob/v7_0_4/Documentation/api/AUTHENTICATION.md), [FHIR API](https://github.com/openemr/openemr/blob/v7_0_4/Documentation/api/FHIR_API.md) and [standard API routes](https://github.com/openemr/openemr/blob/v7_0_4/apis/routes/_rest_routes_standard.inc.php).
- [OpenEMR Docker deployment](https://github.com/openemr/openemr/blob/v7_0_4/DOCKER_README.md) and [HAPI public test server](https://hapi.fhir.org/).
