# Synthetic FHIR integration design

Build a public, reproducible integration service for Jack Lai's portfolio. The repository starts with a README only; there are no existing tests or application behaviors.

Use Python 3.12+, FastAPI and httpx. A run UUID identifies a fixed synthetic Patient, Encounter, and three LOINC/UCUM vital-sign Observations. The bridge checks the upstream R4 CapabilityStatement, performs repeatable PUTs, reads resources back and checks their values and references, and deletes only resources bearing this project's synthetic marker. Failed multi-request runs are explicitly partial and can be retried. No endpoint accepts clinical payloads.

OpenEMR requires a confidential client for user scopes; use a generated, uncommitted client secret with client_secret_post plus PKCE. Public clients remain supported for compatible EHRs. SMART standalone authorization uses discovered endpoints, authorization code + S256 PKCE, expiring browser-bound state, server-side tokens, refresh and logout. The upstream URL is operator configuration, never request input. TLS is verified by default. The localhost-only OpenEMR compose profile is a separate read/auth demonstration: OpenEMR's supported FHIR operations must be checked rather than assuming HAPI's writes exist there. HAPI supplies the public FHIR read/write round trip. A fixed synthetic fixture can be seeded into the disposable OpenEMR instance for patient/encounter/observation reads.

Alternatives: a CLI alone is smaller but less reusable; a frontend adds presentation work without stronger integration evidence. Choose a small HTTP service with OpenAPI docs and a command-line smoke script. This implementation choice is the agent's, under the user's explicit instruction to proceed without questions.

Constraints: synthetic data only, no committed credentials, no main pushes, CI and tests before merge, preserve other worktrees and processes. No claim of production readiness, clinical decision support, or OpenEMR FHIR writes it does not advertise. Public demo means reproducible public source plus a real public HAPI round trip; no paid hosting is required.
