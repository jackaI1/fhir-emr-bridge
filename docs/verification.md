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

[Successful real integration run](https://github.com/jackaI1/fhir-emr-bridge/actions/runs/41773494411) on commit `8dda48d` passed all four jobs: Python 3.12 tests, Python 3.13 tests, built-container health check, and OpenEMR integration. Each Python job ran 50 tests. The initial merged implementation also passed the same four jobs on main in [run 41774319377](https://github.com/jackaI1/fhir-emr-bridge/actions/runs/41774319377), including numeric/UCUM assertions.

The OpenEMR job booted the unmodified `openemr/openemr:7.0.4` image and MariaDB 11.4 with generated local credentials, then executed `scripts/smoke_openemr.py`:

- Dynamically registered a confidential client, enabled it through the disposable instance's CLI configuration, and completed actual admin login and scope consent.
- Exchanged the authorization code using S256 PKCE and `client_secret_post`; the application's callback succeeded without returning tokens.
- Created a synthetic Patient, Encounter and vitals form through the OpenEMR standard API.
- Read FHIR R4 (4.0.1) Patient, Encounter and Observation resources using the bearer token. The report contained one encounter and 15 Observations: three populated numeric vital signs, ten entries with explicit data-absent reasons and two other non-numeric entries.
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

## Readable summaries and repeat check (2026-11-04 UTC / November 3 Toronto)

The follow-up keeps the original 50 tests and adds three cases for distinguishing numeric measurements from missing data, accepting a numeric zero, and handling an empty observation list. Local validation: **53 passed**, Ruff lint passed, and all 14 Python files passed the formatting check.

Repeated the real HAPI command with run ID `7292c0dd-0a40-4b6c-b2ef-79d91e954a7f`. It again reported `verified: true`, `resource_count: 5`, `repeat_writes: 2`, and finally `deleted: 5`. The test launched no local listener or browser.

OpenEMR summaries now preserve each observation's explicit `dataAbsentReason` and report numeric and data-absent counts separately from the total. Missing values without a reason are not classified as explicitly absent. The integration smoke asserts exactly three numeric measurements and stable counts on a second authenticated read. The existing real OpenEMR CI job exercises this addition against the unmodified server image.

### Exact missing-data counts from the follow-up CI run

[PR #2 integration run](https://github.com/jackaI1/fhir-emr-bridge/actions/runs/41775398482) passed all four jobs. Its new summary fields measured **15 total observations, 3 numeric quantities and 10 explicit absent-data reasons**. The other two entries have no numeric quantity or explicit absent-data reason; they must not be described as additional measured vitals or counted as explicitly absent. The two corresponding codes in the summary were `85353-1` and `8327-9`.

The follow-up correction makes the smoke assert 15/3/10 exactly for the pinned OpenEMR 7.0.4 fixture and expands the existing mixed-summary regression case to the same 15-entry shape. The suite remains 53 tests. The successful CI cleanup log confirms removal of both OpenEMR/MariaDB containers, both volumes and the Compose network.

## Visit-report check, November 4, 2026

The visit-report extension passed 89 local automated tests, Ruff lint and Ruff formatting checks on Python 3.12.14. This includes 36 added cases for visit membership, component values, missing-data reasons, status handling, patient isolation, duplicate identities, pagination and route authorization.

I also ran `uv run python scripts/smoke_hapi.py` against `https://hapi.fhir.org/baseR4`. Run `82333d19-4497-48f0-ab62-eec4a7bd78d0` wrote the five fixed fictional resources twice, read back one visit containing all three observations and reported no unassigned or excluded observations. The `finally` cleanup confirmed five deletions. The script created no listener, local server or browser page. Public-server deletion does not guarantee removal of historical versions.

The OpenEMR smoke check now requests the visit-report route after authenticated reads. It checks all 15 observation entries, three original numeric quantities and ten absent-data reasons across the report groups. CI runs that check against actual disposable OpenEMR and MariaDB containers and removes them afterward. A record system's missing encounter link stays visible as unassigned; a timestamp is never used as a substitute.

## Encrypted reports and access history, November 5, 2026

Local validation on Python 3.12.14: **129 tests passed**, including 40 added privacy cases. Ruff lint and formatting checks passed for all 25 Python files, and `uv build` produced both a source distribution and wheel.

`uv run python scripts/smoke_privacy.py` exercised the actual HTTP routes, encrypted SQLite fields and offline wrapping-key rotation with an in-process synthetic FHIR upstream. It reported seven encrypted HAPI report fields, nine valid audit events before deliberate tampering, successful post-rotation readback and detected tampering. Its temporary directory, database and keys were removed. This offline sandbox does not replace the separate real OpenEMR integration check.

The added tests cover wrong encryption keys, changed envelope fields, cross-record substitution, missing fields, changed expiry metadata, all-or-nothing rotation, encrypted persisted credentials, audit edits/interior deletion, external-checkpoint detection of tail deletion, rollback on audit failure, concurrent audit sequencing, saved-report persistence/deletion, session-owner checks, expiry, operator key permissions and CLI commands. Four gated race tests prove logout/expiry during a pending callback or refresh cannot recreate erased credentials. HTTPS policy tests reject remote HTTP and spoofed forwarding/Host headers and require Secure cookies on HTTPS login.

CI runs the suite and offline walkthrough on Python 3.12 and 3.13. The existing OpenEMR/MariaDB job now also compares encrypted saved summaries and visit reports with the actual authenticated upstream reads. The container check retains its fixed health endpoint; data operations over a Docker published port require TLS when the observed peer is non-loopback. Every disposable CI container and volume is removed in an `always()` cleanup step.

No local listener, browser page or disposable server was launched for these checks. The [assessment](privacy-impact-assessment.md) and [operating guide](privacy-operations.md) distinguish implemented safeguards from clinical-deployment requirements, including independent checkpoints, separate key custody, consent/identity policy and operator-scheduled purge.
