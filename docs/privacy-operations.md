# Operating the encrypted report store

I save the latest fictional report so I can compare it with a later read without keeping a plaintext export. The existing HAPI create/read routes and OpenEMR summary/visit routes save their results automatically. Each top-level field, including a nested visit report, is encrypted separately. OAuth access and refresh tokens use the same store with separate envelopes.

## Two-minute offline check

```sh
uv sync --frozen
uv run python scripts/smoke_privacy.py
```

This uses the actual bridge routes and encrypted SQLite store with an in-process fictional FHIR HTTP sandbox. It makes no external requests, opens no ports or browser pages and removes its temporary keys and database. It prints seven encrypted report fields, successful rotation, detected audit tampering and confirmed cleanup. The sandbox replaces only the upstream server; the storage, cryptography and route code are the real implementation. The separate OpenEMR CI check exercises saved reports after real login and consent.

## Keep reports across restarts

Without settings, the vault and its randomly generated keys live only in memory. For disk storage, generate an owner-only key file and keep it outside the repository:

```sh
uv run python -m fhir_emr_bridge.privacy_cli init --keys "$HOME/.config/fhir-emr-bridge/keys.json"
export BRIDGE_PRIVACY_KEY_FILE="$HOME/.config/fhir-emr-bridge/keys.json"
export BRIDGE_PRIVACY_DB_PATH=.local/privacy.sqlite3
uv run uvicorn fhir_emr_bridge.app:app --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

`init` refuses to overwrite an existing key file. The key file and database must have mode 600, meaning only the owner can read or write them. New parent directories use mode 700. SQLite and its rollback journal contain encrypted field values; keys are not in the database. The key file contains a versioned AES wrapping-key ring and a separate audit key. This local provider demonstrates envelope encryption, where a master key protects each field's individual data key. It does not offer the isolation of a managed key service.

In another terminal, use the README's HAPI run commands, then read the saved copy:

```sh
RUN_ID=$(uv run python -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body -X POST "http://127.0.0.1:8000/demo/$RUN_ID"
curl --fail-with-body "http://127.0.0.1:8000/stored/demo/$RUN_ID"
curl --fail-with-body -X DELETE "http://127.0.0.1:8000/demo/$RUN_ID"
```

Stop the service with Ctrl-C. Deleting the upstream demo successfully also deletes that session's local HAPI copy. If the upstream is unavailable, remove just the local copy with `DELETE /stored/demo/{run_id}`. Local deletion does not delete upstream records or their histories.

For OpenEMR, use the existing [login guide](integration.md), read `GET /openemr/patients/{patient_id}` and `/visits`, then use `/stored/openemr-summary/{patient_id}` or `/stored/openemr-visits/{patient_id}`. The browser must still have the same authorized session. A new login cannot read a previous session's copy. Saved routes are not a shortcut around SMART authorization: they may need the upstream token endpoint to refresh an expired token. Session metadata is deliberately not resumed after restart, even in disk mode. Anonymous HAPI mode uses one shared local fictional-demo owner and can read its saved reports after restart.

## TLS and browser settings

TLS is the encrypted connection behind HTTPS. The bridge checks upstream certificates by default. Plain HTTP upstreams and disabled certificate checks are permitted only for loopback demo hosts. SMART discovery must stay on that configured origin, and upstream redirects are not followed.

Incoming HTTP data/auth requests are accepted only from a loopback peer for local demos. Remote HTTP receives 426 before any data access. The fixed `/health` response is the only HTTP exception. `Host: localhost` or `X-Forwarded-Proto: https` cannot change this peer check. TestClient's in-process `testclient` peer is also allowed for automated tests. An HTTPS callback URI or `BRIDGE_REQUIRE_HTTPS=true` requires HTTPS for local data requests too. Cookies get `Secure` whenever login uses HTTPS or HTTPS is required; HTTPS responses set HSTS.

For a direct TLS demo, supply your certificate and private key to Uvicorn:

```sh
export BRIDGE_REQUIRE_HTTPS=true
export BRIDGE_REDIRECT_URI=https://localhost:8443/auth/callback
uv run uvicorn fhir_emr_bridge.app:app --host 127.0.0.1 --port 8443 --no-access-log --no-proxy-headers --ssl-certfile /path/to/localhost-cert.pem --ssl-keyfile /path/to/localhost-key.pem
```

Use a certificate trusted by your client and register the exact HTTPS callback with the EHR. Keep the private key outside git. This command is a deployment example; the offline check needs no certificate. Do not disable upstream certificate verification to solve a browser certificate problem.

Keep `--no-proxy-headers` in direct deployments so the application sees the real peer and scheme. A reverse proxy changes these assumptions: a local TLS terminator connecting by HTTP can look like a local demo client. A proxy deployment must explicitly configure trusted proxy addresses, prevent direct ingress to the app, enforce HTTPS at the edge, use HTTPS callback/cookie settings and be assessed separately. Never trust forwarding headers from arbitrary clients. Docker's published localhost port can appear as a non-loopback peer inside the container; `/health` works, but data requests need a suitable TLS setup. The Python localhost workflow above remains the simple HTTP demo.

## Rotate wrapping keys offline

Stop the bridge first. Do not change keys under a running process, which has its own loaded key ring. Make protected backups of both the encrypted database and key file, stored separately. Then:

```sh
uv run python -m fhir_emr_bridge.privacy_cli add-key --keys "$BRIDGE_PRIVACY_KEY_FILE" --id v2
uv run python -m fhir_emr_bridge.privacy_cli rotate --keys "$BRIDGE_PRIVACY_KEY_FILE" --db "$BRIDGE_PRIVACY_DB_PATH"
uv run python -m fhir_emr_bridge.privacy_cli verify --keys "$BRIDGE_PRIVACY_KEY_FILE" --db "$BRIDGE_PRIVACY_DB_PATH"
```

Choose a new unique ID for every later rotation. `add-key` atomically saves a new active wrapping key while keeping the old keys. `rotate` authenticates and rewraps all saved field data keys in one SQLite transaction, including any remaining encrypted tokens. It also authenticates the clinical ciphertext, leaving that ciphertext and its data nonce unchanged. A failure rolls back all rows and the rotation audit event. Restart only after success. If the rotation fails, the ring still contains old keys; correct the cause and retry with the service stopped.

Keep old wrapping keys until you have tested a restored copy with only the new key, and accounted for older encrypted backups. To retire one, remove only its entry from the private JSON `keys` object while the service is stopped; preserve `active`, the active key and `audit`. The tests demonstrate decrypting all rotated records with the new wrapping key alone. Never edit key bytes in place or reuse a key ID with different bytes.

This is planned wrapping-key rotation. It does not repair a past compromise where someone already copied keys and ciphertext. That requires breach investigation and, for future data, fresh data keys. Do not rotate the audit key in the same operation: retained audit history and keyed identifiers depend on it. An independently anchored new audit segment is future deployment work.

## Verify and anchor the audit history

The audit records request attempts and outcomes, encrypted-store reads/writes/deletions, purge and key rotation. Session and record identifiers are keyed pseudonyms, not raw UUIDs or cookies. Clinical values, authorization codes and token bodies are omitted. A separate HMAC key produces a message-authentication code over each event, sequence and preceding event's code. Editing or removing an interior event breaks verification. Storage changes and their audit entries commit together; an audit failure stops disclosure.

Create a signed checkpoint on a separately protected destination, for example a mounted evidence drive:

```sh
uv run python -m fhir_emr_bridge.privacy_cli checkpoint --keys "$BRIDGE_PRIVACY_KEY_FILE" --db "$BRIDGE_PRIVACY_DB_PATH" --output /trusted/evidence/bridge-head-001.json
uv run python -m fhir_emr_bridge.privacy_cli verify --keys "$BRIDGE_PRIVACY_KEY_FILE" --db "$BRIDGE_PRIVACY_DB_PATH" --checkpoint /trusted/evidence/bridge-head-001.json
```

The destination directory must already exist. Use a fresh checkpoint filename each time; the command refuses to overwrite one. Preserve the latest trusted checkpoint independently of the database host. Later valid events are accepted. A shorter history or different prefix is rejected. Without that external reference, restoring an older valid database or removing its tail is not detectable. Anyone who controls the audit key can forge local history, so this is tamper evidence rather than immutable storage.

Automatic request checks verify the local chain but do not load an external checkpoint. An operator must compare that independent reference before trusting a recovered vault. Full-chain verification is linear in history size and happens on each transaction, which suits a small lab. It needs a different audit architecture at clinical scale.

## Retention, purge and recovery

Reports expire after 24 hours from the latest save. `BRIDGE_REPORT_RETENTION_SECONDS` accepts 1 through 2,592,000 seconds (30 days). Expired reports cannot be read; expired ciphertext is physically deleted at startup or by this operator command:

```sh
uv run python -m fhir_emr_bridge.privacy_cli purge --keys "$BRIDGE_PRIVACY_KEY_FILE" --db "$BRIDGE_PRIVACY_DB_PATH"
```

Run purge through your own approved schedule if the service stays up; the bridge starts no background cleanup process. Tokens expire eight hours after their latest exchange/refresh and are removed on logout, session pruning or graceful shutdown. An unclean stop can leave unreachable ciphertext until purge. The audit remains after deletion, so removing a report does not hide its access history.

For a restore, stop the app, restore the encrypted database and matching protected key file to an isolated location, run `verify --checkpoint` using the separately retained head, and try known fictional saved reports before resuming. A restore older than the trusted checkpoint intentionally fails verification. Lost keys mean lost data. Do not regenerate a key file and expect old records to decrypt. Secure deletion does not erase copied files, SSD snapshots or backups.

For complete fictional-demo cleanup, stop the service, remove `.local/privacy.sqlite3` and its journal if present, remove unused local keys/checkpoints and delete disposable upstream volumes as described in the README. Preserve originals and anything still needed by another run. The [privacy impact assessment](privacy-impact-assessment.md) records the full data flows, retention boundaries, breach procedure and remaining risks.
