# fhir-emr-bridge

I built a small tool for checking medical-record transfers because I needed to test my integration code without using anyone's medical information.

## Overview

Medical-record software stores a patient's details, visits and measurements. A successful save request does not prove that another program can read the right values or connect them to the right visit. I wanted a repeatable way to check the whole trip.

I use this personal project to create an obviously fictional patient, add one visit with weight, pulse and temperature, then read the records back and check their values, units and links. The quickest demo uses a shared public test server and removes its five records afterward. I can also run OpenEMR, a free electronic health record system, on my own computer and test its sign-in and record access.

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

When finished, remove this demo's containers and fictional database:

```sh
docker compose -f compose.openemr.yaml down -v --remove-orphans
```

The database persists until that command. Delete the generated `.env` and `.local/openemr-client-id` / `.local/openemr-client-secret` when no longer needed. The setup binds only to localhost, keeps the database port private and uses OpenEMR 7.0.4 with MariaDB 11.4. It is for development with fictional records.

## Use the service yourself

To send individual requests against HAPI, start the Python web service:

```sh
uv run uvicorn fhir_emr_bridge.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open [the interactive request page](http://127.0.0.1:8000/docs), or run these commands in another terminal. The unique run ID lets me repeat or remove exactly the same five records:

```sh
RUN_ID=$(uv run python -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body -X POST "http://127.0.0.1:8000/demo/$RUN_ID"
curl --fail-with-body "http://127.0.0.1:8000/demo/$RUN_ID"
curl --fail-with-body -X DELETE "http://127.0.0.1:8000/demo/$RUN_ID"
```

Stop the service with Ctrl-C. The API accepts run IDs and generates fixed fictional records; it does not accept names, uploaded records or clinical payloads. For the Docker version, interactive OpenEMR sign-in, route descriptions and configuration, see [the integration guide](docs/integration.md).

## How I check the results

The service uses FastAPI, a Python web framework, to expose its requests. For OpenEMR, SMART on FHIR is the permission process that lets an app access records after login and consent. A one-use proof called PKCE protects the returned authorization code. Access tokens stay in server memory and refresh before they expire. The [integration guide](docs/integration.md) explains the files, authorization flow, repeatable writes and cleanup boundaries.

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

There are 53 automated tests: the original 50 plus three cases checking measured, missing and empty observation summaries. The default suite runs without a network connection. [GitHub Actions](https://github.com/jackaI1/fhir-emr-bridge/actions) runs it on Python 3.12 and 3.13, checks a built service container, and starts real OpenEMR and MariaDB containers for the sign-in and record check. CI removes its containers and volumes afterward. The public HAPI check is a separate command because it depends on a shared server.

I keep measured results and limitations in [the verification notes](docs/verification.md). I use one server worker on localhost because login sessions live in memory; restarting it requires signing in again. This project is not configured to handle real patient records or serve as a public clinical system.
