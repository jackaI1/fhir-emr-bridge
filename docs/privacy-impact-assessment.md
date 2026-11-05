# Privacy impact assessment: fictional FHIR bridge

I wrote this assessment to make the privacy decisions in my bridge reviewable, including the parts that encryption cannot solve. Assessment date: November 5, 2026. Scope: the single-worker Python bridge, its saved reports, SMART login credentials and the disposable OpenEMR/HAPI demonstrations.

This is a PHIPA-style engineering assessment for a synthetic-data project. PHIPA is Ontario's Personal Health Information Protection Act. HIPAA is the US Health Insurance Portability and Accountability Act, sometimes misspelled HIPA. Neither this document nor passing tests certify compliance. I have not assessed a healthcare organization's legal authority, contracts, clinical workflows or physical security. Real patient records remain outside this project's permitted use.

## Responsibility and purpose

I am the local demo operator and maintainer. There is no real health information custodian or clinical privacy officer for this fictional dataset. Before clinical use, the organization responsible for the records would need to name its custodian, PHIPA contact person, technical owner, authorized agents and incident lead, and approve a new assessment for its own deployment.

My purposes are to check record transfer, preserve measurement-to-visit links, compare a saved report with a later upstream read, and investigate demo access failures. I do not use the data for diagnosis, profiling, advertising, research about people or unrelated disclosures. The bridge takes UUIDs, generates fixed fictional records and refuses OpenEMR patient reads unless the patient matches its fixed fictional name and birth date. These checks reduce accidental access; they do not establish legal consent or prove that an upstream database contains no real information.

A real custodian would have to establish the authority and consent for collection, use and disclosure, limit access to the minimum necessary, define agent responsibilities, and provide access/correction and complaint handling. OAuth consent grants an app technical permission; it is not, on its own, a PHIPA consent determination. The IPC's [collection, use and disclosure guidance](https://www.ipc.on.ca/en/health-organizations/collection-use-and-disclosure-of-personal-health-information) is the starting point for that organizational review.

## Data inventory and flows

| Stage | Information and purpose | Source, destination and trust boundary | Implementation |
| --- | --- | --- | --- |
| Fixture creation | Fixed fictional name/date of birth, visit, weight, pulse, temperature and UUIDs | `synthetic.py` to a public HAPI test server, or native API writes to a disposable local OpenEMR database | `fhir.py:write_run`, `openemr.py:seed_demo`; no arbitrary patient upload |
| Delegated login | One-use state, PKCE proof, authorization code, access and refresh tokens, optional registered client secret | Browser to OpenEMR and callback; bridge to the same configured authorization origin | `smart.py:begin/complete/token`; browser gets an opaque HttpOnly cookie, never OAuth tokens |
| FHIR read | Patient/visit links, measurements, units, status and missing-data reasons | Configured upstream over HTTPS to bridge memory, then response to browser | `fhir.py:read_run`, `openemr.py:patient_records`, `reports.py:build_visit_report` |
| Saved report | Seven top-level fields for HAPI; summary/report fields for OpenEMR, including nested clinical values | Bridge memory to encrypted SQLite fields; default SQLite is in memory, explicit operator settings enable disk | `app.py:save`, `vault.py:put/get`; each top-level value, including a whole nested report, gets its own envelope |
| Credential storage | Access and refresh token as two separately encrypted fields | Token response to vault; decrypt only for refresh or an upstream Authorization header | `smart.py:save_tokens/token/logout`; raw tokens are absent from session dataclass representations |
| Local report retrieval | Saved values, not a fresh upstream clinical query | Vault to the creating session; anonymous mode is one local fictional-demo actor | `GET /stored/{kind}/{uuid}`; SMART mode checks a live authorized session and may refresh its token |
| Audit | UTC epoch time, sequence, action, outcome, keyed pseudonyms for session and record/path | Bridge to SQLite audit table; signed checkpoint copied by operator to a separate trusted location | `vault.py:audit/verify/checkpoint`, `app.py:protect_browser_requests` |

The shared HAPI server is public and outside my control. It is not a suitable recipient of identifiable health information. Its hosting location, history retention and other users are outside this assessment. OpenEMR and MariaDB volumes also store the fictional upstream data; bridge field encryption does not encrypt those systems. GitHub receives source, fixed fictional fixtures and test results only. No vault, key file, real record or generated credential belongs in git.

## Safeguards mapped to code and evidence

| Concern | Implemented safeguard | Evidence and remaining boundary |
| --- | --- | --- |
| Disclosure of a copied bridge database | AES-256-GCM authenticated encryption using the `cryptography` library. Fresh 32-byte data key per top-level field; master key wraps that data key with a separate random 12-byte nonce. Encryption keys are never stored in SQLite. | `crypto.py:EnvelopeCipher`; `test_privacy.py` covers randomization, wrong keys and altered ciphertext. Fields, sizes, namespace, expiry times and pseudonyms remain visible. |
| Moving ciphertext between records or fields | Authenticated associated data binds format version, namespace, keyed record ID, session owner, expiry and field name. A separate keyed row signature authenticates the full field set. | `vault.py:_row/put/get`; tests reject changed expiry, field deletion and context substitution. Deleting a whole record can still deny availability. |
| Rotation and key separation | Versioned wrapping keys; offline atomic rewrapping of every data key. Clinical ciphertext stays unchanged. A separate 256-bit HMAC key signs audit and row metadata using distinct domain labels. | `vault.py:rotate`, `privacy_cli.py:add_key`; successful rotation reads using only the new wrapping key; failure rolls back all rows. This is a local key-file provider, not a managed key service. |
| Unauthorized saved-report access | Records are scoped to the session that saved them. Existing SMART authorization gates remain on data routes. Only allowed report namespaces and UUIDs are accepted. | `app.py:stored_record`, `vault.py:get`; API tests cover missing login; vault tests reject another owner. Session pseudonyms identify a browser session, not a verified named clinician. |
| Network interception | Upstream HTTP and certificate-verification bypass are limited to loopback demo servers. Incoming remote HTTP gets 426 before data access; HTTPS callback mode or `BRIDGE_REQUIRE_HTTPS=true` requires HTTPS for local data requests too. Health exposes only fixed status. | `config.py:safe_url`, `app.py:protect_browser_requests`; tests cover remote HTTP, spoofed Host/forwarding headers and HTTPS headers. Use direct TLS with `--no-proxy-headers`; a TLS proxy requires a separately assessed trust configuration. |
| Undetected record access or edits to history | A durable request-attempt event precedes each data route, including reads that happen during writes/deletes. A second event records HTTP outcome. Storage reads, writes, deletion and rotation are also logged. Each audit HMAC includes the preceding HMAC and sequence. Record mutations and their audit entries share a transaction. | `vault.py:transaction/_append/verify`; tests alter events, delete entries, forge signatures and break audit writes. Corrupt/unwritable audit blocks record requests before upstream access. An upstream mutation cannot be undone if logging fails after the upstream call. |
| Audit rollback or administrator tampering | Signed checkpoints bind a known count and head. Verification permits later valid entries but rejects deletion of the checkpointed suffix. | `privacy_cli.py:checkpoint/verify`; checkpoint test deletes the tail. Without a separately retained checkpoint, suffix deletion or restoring an older valid database cannot be distinguished from shorter legitimate history. An attacker with the audit key can forge history. This is tamper evidence, not an immutable external ledger. |
| Accidental logs and browser caching | No clinical values, token bodies, raw patient IDs or cookie values in audit events. Keyed pseudonyms resist guessing identifiers without the key. Responses use no-store and no-referrer; errors are sanitized; HTTPS responses set HSTS. | `app.py`, `vault.py:opaque`, `fhir.py:request_json`; disk-plaintext and response tests. Keep Uvicorn access logging disabled; a debugger, crash dump or compromised live process can still see plaintext. |

The audit chain is verified before every storage transaction and request event. That is deliberately simple and increasingly expensive as history grows. This is a small single-worker lab, not a scalable hospital audit service. Remote audit collection, access monitoring, alerting, independent key custody, load limits and backups are deployment work still required.

## Retention and disposal

| Data | Demo retention rule | Disposal and limits |
| --- | --- | --- |
| Pending login proof | Ten minutes, bounded to 128 sessions | Pruned during session operations; cleared after code exchange; lost on restart |
| OAuth credentials | Encrypted record expires eight hours after a successful exchange/refresh; browser cookie lasts eight hours from login | Logout, expiry pruning and graceful shutdown delete token records. Session metadata is not restored on restart; after an unclean stop, unreachable encrypted token rows remain until expiry/purge. Logout does not revoke the upstream grant. |
| Saved reports | 24 hours after latest successful read/write by default; configurable 1 second to 30 days | Reads refuse expired records. Startup and operator `purge` remove expired rows. `DELETE /stored/...` removes the current session's copy immediately; successful HAPI DELETE removes that session's saved HAPI report. Purge is not a background scheduler. Other session copies and backups need their own disposal. |
| Audit history and checkpoints | Retained for the life of the demo vault | Individual entries are not pruned because that would break history verification. For a disposable fictional run, stop service, verify/archive if needed, then remove the whole vault and unused keys. Clinical audit retention must be approved by the custodian; no demo duration is presented as a PHIPA legal minimum. |
| OpenEMR/HAPI source records | Separate from the bridge cache | `docker compose -f compose.openemr.yaml down -v --remove-orphans` removes disposable volumes. HAPI DELETE is logical deletion and may preserve history. |

SQLite `secure_delete` overwrites deleted database content, but it cannot guarantee erasure from SSD snapshots, backups, swap or copied files. Keep key backups separate from encrypted database backups and use a protected encrypted disk. Destroying an old wrapping key after rewrapping does not erase old backups that still depend on that key. Protect or destroy those backups under the same retention decision. The audit key must remain available to verify retained history; its rotation would require a new, externally linked audit segment, which this implementation does not automate.

## Breach response exercise

I use this procedure for a fictional incident. A real custodian must assess its notification duties with its privacy officer. The IPC recommends a breach protocol with immediate containment, investigation and prevention work, alongside notification where required. Certain breaches must be reported to the IPC at the first reasonable opportunity; encryption alone does not settle whether notification is required. See the IPC's [health privacy breach protocol](https://www.ipc.on.ca/en/health-organizations/responding-to-a-privacy-breach/privacy-breach-protocol) and [reporting guidance](https://www.ipc.on.ca/en/health-organizations/report-a-privacy-breach).

1. Stop the bridge and isolate the affected vault. Preserve an access-controlled copy of the encrypted database, the latest independently stored signed checkpoint and relevant bounded operational logs. Record discovery time, affected environment and who handled evidence. Do not upload evidence to a public issue.
2. Notify the incident lead and PHIPA contact person. Determine whether only ciphertext escaped, whether wrapping/audit keys or a live process were exposed, and which upstream permissions were available. Treat session pseudonyms and audit metadata as potentially sensitive.
3. Revoke the upstream authorization grant using the EHR's administration tools and terminate affected sessions. Local logout alone is insufficient. If only a wrapping key needs planned replacement, use the documented offline rotation. If a master key and database were stolen, rewrapping cannot undo past disclosure; use fresh data keys and investigate the exposure. An exposed audit key invalidates trust in locally reconstructed history.
4. Compare the preserved audit with the external checkpoint, identify affected records and access windows, and document uncertainty. Never repair evidence in place or describe a missing checkpoint as proof of no access.
5. The custodian determines affected-individual, IPC and any other required notices, timing and contents, and documents the decision. This tool sends no automatic notifications and makes no legal determination.
6. Fix the cause, rerun tests, validate restore and key access in an isolated environment, and have the responsible owner approve resumption. Record preventive changes and revisit this assessment.

## Residual risks and decision

| Risk | Residual severity for real health data | Required owner/action before clinical use |
| --- | --- | --- |
| Key file or live host compromise reveals data and permits forged audit | High | Security owner: managed key service, separation of duties, host hardening and remote append-only audit/checkpoints |
| No verified clinician identity, role policy, patient consent service or access/correction workflow | High | Custodian/privacy officer: authorize purposes, scopes, agent access and rights handling; technical owner: enforce it |
| Public upstream, unknown residency, uncontrolled source histories | High | Custodian: approved EHR/hosting agreements and recipient/residency review; never real data on public HAPI |
| Expired ciphertext or audit accumulates; full verification grows with history | Medium | Operator: scheduled purge and monitored capacity; clinical owner: approved backup, audit and clinical-record retention schedules |
| Loss of keys/database or incorrect recovery | High | Operator: separately protected backups and tested recovery; keys are intentionally not recoverable from the vault |
| Record replay/deletion or whole-history rollback | High | Security owner: independent checkpoints, protected backups and external immutable audit. Row authentication does not provide database-wide freshness. |

My decision is to use these safeguards for fictional demonstrations only. The repository demonstrates implemented privacy controls and an explicit assessment of the gaps. It is not approved for clinical deployment, PHIPA compliance certification or HIPAA certification. HHS describes risk analysis as foundational to choosing safeguards; it covers administrative and physical controls as well as code. See [HHS risk-analysis guidance](https://www.hhs.gov/hipaa/for-professionals/security/guidance/guidance-risk-analysis/index.html).

## Reproduce the assessment evidence

```sh
uv sync --frozen
uv run python scripts/smoke_privacy.py
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

`tests/test_privacy.py` exercises cryptography, ciphertext substitution, expiry metadata, key rotation including rollback, concurrent audit sequencing and audit alteration. `tests/test_privacy_api.py` exercises actual route storage, restart, login/refresh/logout and TLS enforcement. `tests/test_privacy_cli.py` runs the operator commands as subprocesses. CI additionally verifies encrypted saved reports after real OpenEMR login and consent. The [operating guide](privacy-operations.md) has the exact key, TLS, rotation, purge and checkpoint commands.
