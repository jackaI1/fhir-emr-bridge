# fhir-emr-bridge

I built a small tool for checking medical-record transfers because I needed to test my integration code without using anyone's medical information.

Medical-record software stores a patient's details, visits and measurements. A successful save request does not prove that another program can read the right values or connect them to the right visit. I wanted a repeatable way to check the whole trip.

I use this personal project to create an obviously fictional patient, add one visit with weight, pulse and temperature, then read the records back and check their values, units and links. The quickest demo uses a shared public test server and removes its five records afterward. I can also run OpenEMR, a free electronic health record system, on my own computer and test its sign-in and record access.

I save the latest fictional reports in an encrypted store so I can compare them later without leaving readable patient values on disk. Login credentials use the same protection, and an access history helps me check who opened a record. Run `uv run python scripts/smoke_privacy.py` after setup for an offline walkthrough that removes its temporary files.

I also use the visit report to check which measurements belong together. It follows each record's saved visit link, keeps missing measurements visible and sets aside cancelled or erroneous results. Two visits on the same date stay separate.

The programs exchange records using Fast Healthcare Interoperability Resources (FHIR), a common format for healthcare data. This project uses release 4, called R4. Start with the public-server check below; it needs no account or local record system.

## Try a fictional visit

Install [Python 3.12 or newer](https://www.python.org/downloads/) and [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs and runs the project's Python dependencies. You also need Git and an internet connection.

```sh
git clone https://github.com/jackaI1/fhir-emr-bridge.git
cd fhir-emr-bridge
uv sync --frozen
uv run python scripts/smoke_hapi.py
```

The script uses the [HAPI public test server](https://hapi.fhir.org/). It creates one patient, one visit and three measurements, repeats the same writes, then reads everything back. Success prints `"verified": true`, `"resource_count": 5`, `"repeat_writes": 2` and finally `"deleted": 5`. Cleanup runs even if a check fails. The script opens no ports or browser pages.

Look for `visit_report` in that output. Its `counts` show three records assigned to one visit, zero unassigned and zero excluded. Each result keeps its original value and unit. This is the quickest way to try the report without setting up a local medical-record system.

This is a shared, unauthenticated server that may be unavailable or periodically cleared. Use only the included fictional records. Deletion can leave record history on the server. If a successful report is followed by a cleanup failure, use its `run_id` to retry deletion through the service below.

## Try my local OpenEMR setup

After the Python setup above, install Docker Engine or Docker Desktop with Compose v2 or newer. Docker runs OpenEMR and its MariaDB database in isolated containers. Allow about 2 GB of container memory and leave port 9300 free. On macOS, a separately managed Colima virtual machine also works.

```sh
uv run python scripts/demo_env.py
docker compose -f compose.openemr.yaml up -d --wait --wait-timeout 600
uv run python scripts/smoke_openemr.py
```

The first command generates local passwords in ignored `.env`; it refuses to replace an existing file. Startup can take several minutes. The check signs in to OpenEMR, approves access for the bridge, creates the fictional visit and reads it back. Each run creates a new fictional patient.

Success prints `"smart_authorized": true`, `"fhir_version": "4.0.1"`, one encounter (visit), and `"numeric_observation_count": 3`. With OpenEMR 7.0.4, there are 15 observation entries: three numeric vital signs, ten entries marked as missing data and two other entries without numeric quantities. The other twelve entries are not twelve additional numeric measurements. The output preserves the reasons in `data_absent_reason` and reports their count in `data_absent_observation_count`.

OpenEMR 7.0.4 only supports reading these records through its FHIR interface. I create them through OpenEMR's standard API, its other program-facing interface, then check the FHIR reads. The public HAPI demo separately tests FHIR writes.

The check also requests a visit report and prints `visit_report_counts`. It accounts for all 15 observation entries and checks that the same three quantities and ten missing-data reasons survive. A record without a usable visit link appears under `unassigned_observations`; the report never fills in a link from its timestamp.

When finished, remove this demo's containers and fictional database:

```sh
docker compose -f compose.openemr.yaml down -v --remove-orphans
```

The database persists until that command. Delete the generated `.env` and `.local/openemr-client-id` / `.local/openemr-client-secret` when no longer needed. The setup binds only to localhost, keeps the database port private and uses OpenEMR 7.0.4 with MariaDB 11.4. It is for development with fictional records.

## Use the service yourself

To send individual requests against HAPI, start the Python web service:

```sh
uv run uvicorn fhir_emr_bridge.app:app --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

Open [the interactive request page](http://127.0.0.1:8000/docs), or run these commands in another terminal. The unique run ID lets me repeat or remove exactly the same five records:

```sh
RUN_ID=$(uv run python -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body -X POST "http://127.0.0.1:8000/demo/$RUN_ID"
curl --fail-with-body "http://127.0.0.1:8000/demo/$RUN_ID"
curl --fail-with-body -X DELETE "http://127.0.0.1:8000/demo/$RUN_ID"
```

Stop the service with Ctrl-C. The API accepts run IDs and generates fixed fictional records; it does not accept names, uploaded records or clinical payloads. For the Docker version, interactive OpenEMR sign-in, route descriptions and configuration, see [the integration guide](docs/integration.md).

## Read a visit report

The HAPI read above includes `visit_report`. For OpenEMR, sign in using the integration guide, create a fictional patient with `POST /openemr/demo`, then open `GET /openemr/patients/{patient_id}/visits` on the interactive request page. Replace `{patient_id}` with the returned patient ID. The existing patient summary remains available at `GET /openemr/patients/{patient_id}`.

The report has a `visits` list, `unassigned_observations` that need review, and `excluded_observations` for cancelled or erroneous records. An observation is a measurement or other recorded result. The counts add up across those three groups, so a missing link cannot quietly remove a result. An empty visit remains visible too.

I keep the source's measurement status and units, including preliminary results, pounds and Fahrenheit. A blood-pressure result keeps both components instead of becoming one number. A missing value stays missing with its recorded reason. The report does not choose a diagnosis, convert units or replace an earlier result with a newer one. [Report details and supported references](docs/visit-reports.md) explain the boundaries.

## Keep a protected copy of a report

My service saves the HAPI readback and OpenEMR summary/visit reports automatically. The default store lasts only until the service stops. To keep encrypted reports across restarts, generate a private key file outside the repository and set the database path using the [storage setup](docs/privacy-operations.md#keep-reports-across-restarts).

After creating a HAPI run through the service, `GET /stored/demo/{run_id}` reads its saved copy. For OpenEMR, use `GET /stored/openemr-summary/{patient_id}` or `GET /stored/openemr-visits/{patient_id}` after reading those reports, in the same signed-in browser. `DELETE` on the same saved-report URL removes only that local copy. Reports expire after 24 hours by default; startup or the purge command removes expired ciphertext. The access history remains.

I use AES-256-GCM, encryption that also detects changed values. Each stored field has its own random data key, wrapped by a versioned master key. Rotating a master key replaces those wrappers without rewriting the report values. A separate secret signs the access history as a chain so edits can be detected. A signed checkpoint saved somewhere independent is needed to detect missing history at the end. Incoming remote requests require HTTPS, and the local HTTP exception is only for the demo.

```sh
uv run python scripts/smoke_privacy.py
```

The offline walkthrough uses a fictional FHIR sandbox inside the process. It checks seven encrypted fields, a saved readback, key rotation and detected audit tampering, then removes its keys and database. It opens no ports or browser pages. The [operating guide](docs/privacy-operations.md) explains disk storage, TLS, rotation, checkpoints and cleanup. My [PHIPA-style privacy impact assessment](docs/privacy-impact-assessment.md) maps the data flows, purposes, safeguards, retention and breach response to the code. PHIPA is Ontario's health-information privacy law; the assessment is an engineering exercise, not a claim that this demo is approved for clinical use or certified under PHIPA or the US HIPAA law.

## How I check the results

The service uses FastAPI, a Python web framework, to expose its requests. For OpenEMR, SMART on FHIR is the permission process that lets an app access records after login and consent. A one-use proof called PKCE protects the returned authorization code. Access and refresh tokens stay encrypted in the server-side store and refresh before they expire. Only expiring session metadata stays in memory. The [integration guide](docs/integration.md) explains the files, authorization flow, repeatable writes and cleanup boundaries.

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

There are 129 automated tests, including 40 added privacy cases and 36 report cases covering visit links, blood-pressure components, absent values, record status, mixed-patient rejection, duplicate identities, pagination and the service routes. The privacy cases cover wrong keys, modified ciphertext, rotation rollback, audit integrity, session isolation, retention, TLS policy and logout during an in-flight token exchange. The default suite runs without a network connection. [GitHub Actions](https://github.com/jackaI1/fhir-emr-bridge/actions) runs it on Python 3.12 and 3.13, checks a built service container, and starts real OpenEMR and MariaDB containers for the sign-in, record and visit-report checks. CI removes its containers and volumes afterward. The public HAPI check is a separate command because it depends on a shared server.

I keep measured results and limitations in [the verification notes](docs/verification.md). I use one server worker on localhost because login sessions live in memory; restarting it requires signing in again. This project is not configured to handle real patient records or serve as a public clinical system.
