# Running and understanding the integration

The [README](../README.md) starts with the fictional-visit workflow. This guide covers the optional server, sign-in settings and implementation details.

## Run the bridge in Docker

Docker packages the Python service and its dependencies into a container. This runs against the shared HAPI test server and needs no account:

```sh
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/health
docker compose down --remove-orphans
```

The container binds only `127.0.0.1:8000`. A healthy process does not guarantee that the upstream record server is available.

## Sign in to the local OpenEMR demo interactively

First complete the OpenEMR smoke check in the README. It saves an application ID and secret in ignored local files. The automated check uses the service in process, so it needs no listening port or browser. For an interactive session afterward:

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

Stop the bridge with Ctrl-C. Stop and remove the disposable OpenEMR instance and its fictional database with:

```sh
docker compose -f compose.openemr.yaml down -v --remove-orphans
```

The volumes retain fictional records until that command. Delete the generated `.env`, `.local/openemr-client-id` and `.local/openemr-client-secret` when no longer needed. Stop a dedicated Colima profile too if you started one for this demo.

## HTTP routes and repeated writes

An API is the interface another program uses to request records. The service exposes these routes:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Process health; does not imply upstream availability |
| `GET /capabilities` | Check upstream FHIR version and advertised capabilities |
| `POST /demo/{uuid}` | Create or update and verify the fixed five-resource synthetic run |
| `GET /demo/{uuid}` | Read and verify the same run |
| `DELETE /demo/{uuid}` | Check ownership of every resource, then delete observations before encounter and patient |
| `GET /auth/login`, `GET /auth/callback` | SMART standalone browser authorization |
| `POST /auth/logout` | Discard the local session and tokens |
| `POST /openemr/demo` | Opt-in local native-API seed followed by FHIR reads |
| `GET /openemr/patients/{uuid}` | Summarize only the fixed synthetic OpenEMR patient |
| `GET /openemr/patients/{uuid}/visits` | Group fictional results by their explicit visit references, retaining missing and excluded results |

`GET /demo/{uuid}` also includes a `visit_report`. See [visit reports](visit-reports.md) for its fields, reference rules and examples. Both paths use the same report builder; the OpenEMR route reuses the patient check, paginated searches and authorization of the existing summary.

IDs derive from the run UUID. Use a fresh UUID per independent run. Writes use instance PUT when `updateCreate` is advertised, or conditional PUT with `_id` when `conditionalUpdate` is advertised and `updateCreate` is omitted. An omitted field means unknown update-as-create support: this path is verified against HAPI, but another server may reject it. Explicit `updateCreate: false` is refused before writing. Requests use `application/fhir+json`. Verification requires all expected clinical fields, synthetic tags, LOINC codes, UCUM quantities and patient/encounter references; server-added metadata is permitted.

The five writes are sequential, **not an atomic FHIR transaction**. An error reports `completed`; repeat the same UUID to recover or use DELETE to clean up. Existing resources without the project tag are never overwritten or deleted. Public HAPI deletion is a logical FHIR deletion, not guaranteed erasure of server history.

## OpenEMR authorization and compatibility

SMART on FHIR describes how an application asks a record system for permission to access records. The bridge uses an authorization code returned after login and consent, rather than accepting the user's password itself. Proof Key for Code Exchange (PKCE) binds that code to a one-use secret held by the bridge. Its S256 method sends a SHA-256 hash at the start and proves possession of the secret when exchanging the code. OpenEMR also requires a registered application secret for these provider permissions. This is confidential-client authentication, sent using `client_secret_post` alongside PKCE. A compatible public client can leave that secret unset.

The smoke script configures API globals only in the disposable instance, dynamically registers and enables a client, and drives the actual login and consent forms over HTTPS. It uses no password grant, fabricated access token or skipped consent. No OpenEMR source files or authorization checks are patched. The setup script generates unique credentials in ignored `.env` with mode 600 and refuses to overwrite an existing file. The client secret is also stored with mode 600.

OpenEMR 7.0.4 exposes reads and searches for these FHIR resources, so creation uses its standard API with the same authorization token. HAPI provides the separate FHIR write demonstration. Each OpenEMR seed creates a new fictional patient. The fixture supplies only weight, pulse and temperature: the calculated blood-pressure subscriber assumes a browser session during standard-API writes and can return HTTP 500. The FHIR reader accepts OpenEMR's legacy `collection` search results as well as standard `searchset` results, while still checking resource types and patient references.

The summary preserves the upstream values and units. LOINC codes identify which measurement was taken; UCUM codes identify its units. OpenEMR returns pulse 68 `/min`, temperature 98.24 `[degF]` and weight 159.83514 `[lb_av]` for this fixture. These are beats per minute, degrees Fahrenheit and pounds. The separate HAPI fixture uses Celsius and kilograms.

`observation_count` includes every returned observation, including empty fields. `numeric_observation_count` counts numeric `valueQuantity.value` fields, including zero. `data_absent_observation_count` counts explicit `dataAbsentReason` entries; an unexplained missing value is not assumed to have a reason. Each summary item preserves that reason in `data_absent_reason`. The live fixture has three numeric measurements, ten observations with explicit missing-data reasons and two other non-numeric entries, for 15 entries total. The smoke check verifies those exact counts and the three quantities, then checks that the counts remain stable on a second read.

## Configuration and security boundaries

Settings use the `BRIDGE_` environment prefix (or ignored `.env`); see [`config.py`](../src/fhir_emr_bridge/config.py). Default upstream: `https://hapi.fhir.org/baseR4`. `BRIDGE_SMART_CLIENT_SECRET` optionally supplies confidential-client authentication and is masked in settings representations. A configured `BRIDGE_SMART_CLIENT_ID` requires a valid browser session before data operations. Additional settings include `BRIDGE_REDIRECT_URI` and JSON-array `BRIDGE_ALLOWED_HOSTS`. HTTP is allowed only on loopback; disabling TLS verification is allowed only for a loopback upstream and is used for OpenEMR's generated demo certificate.

SMART discovery is restricted to the configured upstream origin. State is random, single-use, browser-bound and valid for ten minutes. Tokens remain in bounded server memory, refresh before expiration, and never appear in API responses. Cookies are HttpOnly and SameSite=Lax (Secure for HTTPS callbacks). This is delegated authorization; the service does not consume ID-token identity claims. Logout removes local credentials; it does not revoke the upstream grant. Request origins and hostnames are checked, redirects are not followed by the FHIR client, pagination is bounded to the configured FHIR base, and upstream errors are sanitized. Keep access logging disabled so callback codes are not logged.

This is a development integration demonstrator, not a clinical system. Use one worker on localhost. Internet hosting would require operator authentication/rate limiting, persistent encrypted sessions, HTTPS termination and deployment-specific controls. Do not point it at a real patient database. It does not claim full SMART certification, EHR-launch support, US Core validation or clinical decision support.

## Implementation files

I chose a small HTTP service with an interactive request page and command-line
checks so I can reuse the integration without building a separate frontend.
A command-line tool alone would offer fewer ways to call the same workflow.

| File | Role in the visit workflow |
| --- | --- |
| [`app.py`](../src/fhir_emr_bridge/app.py) | Defines the HTTP routes, starts the shared upstream client and connects requests to a server-side login session. |
| [`smart.py`](../src/fhir_emr_bridge/smart.py) | Requests permission, exchanges authorization codes, refreshes expiring tokens and keeps credentials in bounded server memory. |
| [`openemr.py`](../src/fhir_emr_bridge/openemr.py) | Creates the fictional visit through OpenEMR's standard API and summarizes patient-scoped FHIR searches, including missing-data counts. |
| [`fhir.py`](../src/fhir_emr_bridge/fhir.py) | Checks server capabilities, repeats the five HAPI writes, verifies returned values and deletes only records owned by a demo run. |
| [`synthetic.py`](../src/fhir_emr_bridge/synthetic.py) | Defines the fixed fictional patient, visit and three measurements. |
| [`smoke_openemr.py`](../scripts/smoke_openemr.py) | Completes real local login and consent, seeds a visit and verifies authenticated reads and measurement units. |
| [`smoke_hapi.py`](../scripts/smoke_hapi.py) | Writes the same five records twice on the public server, reads them back and cleans them up in a `finally` block. |
| [`compose.openemr.yaml`](../compose.openemr.yaml) | Runs the disposable OpenEMR and MariaDB containers with memory limits and a localhost-only web port. |

## Protocol references

- [HL7 FHIR R4 REST interactions](https://hl7.org/fhir/R4/http.html) and [SMART App Launch authorization](https://hl7.org/fhir/smart-app-launch/app-launch.html).
- [OpenEMR 7.0.4 authentication](https://github.com/openemr/openemr/blob/v7_0_4/Documentation/api/AUTHENTICATION.md), [FHIR API](https://github.com/openemr/openemr/blob/v7_0_4/Documentation/api/FHIR_API.md) and [standard API routes](https://github.com/openemr/openemr/blob/v7_0_4/apis/routes/_rest_routes_standard.inc.php).
- [OpenEMR Docker deployment](https://github.com/openemr/openemr/blob/v7_0_4/DOCKER_README.md) and [HAPI public test server](https://hapi.fhir.org/).
