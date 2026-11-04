# Integration verification

The repository started at `d8e6b52` with only a README and no existing tests.

## Public HAPI FHIR R4 (2026-11-03)

Executed `uv run python scripts/smoke_hapi.py` through the FastAPI application, against `https://hapi.fhir.org/baseR4` with TLS verification enabled.

- FHIR CapabilityStatement version: 4.0.1.
- HAPI advertises conditional updates; it omits `updateCreate`. Used `PUT ResourceType?_id=...` with fixed, namespaced IDs. Omission means unknown support; this path was verified empirically on HAPI and is not assumed universally. Explicit `updateCreate: false` is rejected.
- Created one synthetic Patient, one linked Encounter, three linked Observations (body weight 72.5 kg, pulse 68/min, temperature 36.8 Cel).
- Repeated the same five upserts, then read all five resources and verified clinical fields, tags and references.
- Report: `verified: true`, `resource_count: 5`, `repeat_writes: 2`.
- Finally cleanup: `deleted: 5`. No local listener or test browser was created. FHIR deletes can retain history on the public server.

## OpenEMR and CI (2026-11-04 UTC / November 3 Toronto)

[Successful real integration run](https://github.com/jackaI1/fhir-emr-bridge/actions/runs/41773494411) on commit `8dda48d` passed all four jobs: Python 3.12 tests, Python 3.13 tests, built-container health check, and OpenEMR integration. Each Python job ran 50 tests. The final PR head reruns the same checks, with additional numeric/UCUM assertions and no duplicate feature-branch push workflow.

The OpenEMR job booted the unmodified `openemr/openemr:7.0.4` image and MariaDB 11.4 with generated local credentials, then executed `scripts/smoke_openemr.py`:

- Dynamically registered a confidential client, enabled it through the disposable instance's CLI configuration, and completed actual admin login and scope consent.
- Exchanged the authorization code using S256 PKCE and `client_secret_post`; the application's callback succeeded without returning tokens.
- Created a synthetic Patient, Encounter and vitals form through the OpenEMR standard API.
- Read FHIR R4 (4.0.1) Patient, Encounter and Observation resources using the bearer token. The report contained one encounter and 15 Observations: three populated numeric vital signs plus OpenEMR's data-absent observations for other fields.
- Verified these returned LOINC/UCUM quantities (preserving the upstream units):

| LOINC | Value | UCUM |
| --- | ---: | --- |
| `8867-4` (pulse) | 68 | `/min` |
| `8310-5` (temperature) | 98.24 | `[degF]` |
| `29463-7` (weight) | 159.83514 | `[lb_av]` |

- Re-read the patient summary and confirmed the same observation count; confirmed the generic FHIR-write demo is rejected before writes on OpenEMR.
- Logged out; the CI cleanup step successfully stopped containers and removed both synthetic-data volumes.

Compatibility findings are explicitly handled: OpenEMR requires confidential clients for provider `user/` scopes; its legacy search Bundles use `collection`; and its calculated-BP subscriber assumes a browser session during standard-API writes. The fixture omits BP and includes only the three required numeric vitals. No OpenEMR authorization checks or source files were patched. HAPI performs the separate FHIR read/write demonstration; OpenEMR native seeding is not presented as FHIR writing.

Default tests use HTTP boundary fixtures and require no network. Public HAPI testing is opt-in. Tests also cover partial-write recovery, ownership protection, incompatible servers, malformed responses, OAuth state replay/expiry/browser binding, concurrent refresh rotation, bounded session capacity, secret-safe configuration errors, pagination, and legacy OpenEMR response shapes.
