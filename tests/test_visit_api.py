from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient
from test_openemr import ENCOUNTER, PATIENT, bundle

from fhir_emr_bridge.app import create_app
from fhir_emr_bridge.config import Settings

BASE = "https://ehr.test/fhir/"


def record(kind, identifier, **fields):
    return {
        "resourceType": kind,
        "id": identifier,
        "subject": {"reference": "Patient/" + PATIENT},
        **fields,
    }


def test_authenticated_visits_require_login_even_for_fictional_patients():
    settings = Settings(fhir_base_url=BASE.rstrip("/"), smart_client_id="demo")
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/openemr/patients/{PATIENT}/visits").status_code == 401
        assert client.get("/openemr/patients/not-a-uuid/visits").status_code == 422


@pytest.mark.parametrize(
    "case", ["valid", "wrong-id", "not-synthetic", "duplicate", "wrong-patient"]
)
def test_visit_route_checks_identity_paginates_and_keeps_absent_values(case):
    calls = []

    def upstream(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/Patient/" + PATIENT):
            return httpx.Response(
                200,
                json={
                    "resourceType": "Patient",
                    "id": PATIENT if case != "wrong-id" else "another-id",
                    "name": [
                        {
                            "family": "SYNTHETIC" if case != "not-synthetic" else "OTHER",
                            "given": ["FHIRBridgeDemo"],
                        }
                    ],
                    "birthDate": "1990-01-01",
                },
            )
        if request.url.path.endswith("/Encounter"):
            return httpx.Response(
                200, json=bundle([record("Encounter", ENCOUNTER, status="finished")])
            )
        observation = record(
            "Observation",
            "pulse",
            status="final",
            encounter={"reference": "Encounter/" + ENCOUNTER},
            code={"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
            valueQuantity={"value": 68, "code": "/min", "system": "http://unitsofmeasure.org"},
        )
        if case == "wrong-patient":
            observation["subject"]["reference"] = "Patient/another"
        if "page" not in request.url.params:
            return httpx.Response(200, json=bundle([observation], BASE + "Observation?page=2"))
        if case != "duplicate":
            observation = deepcopy(observation)
            observation["id"] = "missing-temperature"
            observation.pop("valueQuantity")
            observation["dataAbsentReason"] = {"text": "Not measured in this fictional visit"}
        return httpx.Response(200, json=bundle([observation]))

    app = create_app(
        Settings(fhir_base_url=BASE.rstrip("/")), transport=httpx.MockTransport(upstream)
    )
    with TestClient(app) as client:
        response = client.get(f"/openemr/patients/{PATIENT}/visits")
    if case in {"wrong-id", "not-synthetic"}:
        assert response.status_code == 409
        assert len(calls) == 1  # No clinical searches after a failed patient check.
    elif case in {"duplicate", "wrong-patient"}:
        assert response.status_code == 502
        assert "another" not in response.text
    else:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert result["counts"] == {"total": 2, "assigned": 2, "unassigned": 0, "excluded": 0}
        values = result["visits"][0]["observations"]
        assert values[0]["valueQuantity"]["value"] == 68
        assert "valueQuantity" not in values[1]
        assert values[1]["dataAbsentReason"]["text"].startswith("Not measured")
        assert len(calls) == 4
