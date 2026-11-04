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

## OpenEMR and CI

The CI workflow boots an isolated OpenEMR 7.0.4/MariaDB stack and runs `scripts/smoke_openemr.py`. It performs real dynamic public-client registration, admin enablement, login/consent, authorization-code exchange with S256 PKCE, native synthetic seeding, and authenticated FHIR reads. Results and final CI links will be recorded after execution. All jobs include fixture cleanup.
