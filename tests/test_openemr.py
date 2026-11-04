import json

import httpx
import pytest

from fhir_emr_bridge.fhir import BridgeError, FhirClient
from fhir_emr_bridge.openemr import patient_summary, seed_demo

PATIENT = "66a95d46-df50-4c96-ac47-95ff29d7eadc"
ENCOUNTER = "99d6348c-8fb3-4f8c-acb5-c928a45b7e54"


def bundle(items, next_url=None):
    result = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in items],
    }
    if next_url:
        result["link"] = [{"relation": "next", "url": next_url}]
    return result


def upstream(request):
    assert request.headers["Authorization"] == "Bearer private-token"
    route = request.url.path
    if request.method == "POST":
        payload = json.loads(request.content)
        if route.endswith("/api/patient"):
            assert payload["lname"] == "SYNTHETIC"
            assert payload["DOB"] == "1990-01-01"
            return httpx.Response(201, json={"data": {"pid": 1, "uuid": PATIENT}})
        if route.endswith("/encounter"):
            assert "/patient/" + PATIENT in route
            assert payload["reason"] == "Synthetic integration demonstration"
            return httpx.Response(200, json={"data": {"encounter": 2, "uuid": ENCOUNTER}})
        assert route.endswith("/api/patient/1/encounter/2/vital")
        # Reproduce OpenEMR 7.0.4 calculated-BP API bug seen in the real container.
        if "bps" in payload or "bpd" in payload:
            return httpx.Response(500, json={"error": "missing web-session authUserID"})
        assert payload["pulse"] == "68"
        assert payload["temperature"] == "98.24"  # OpenEMR standard API uses Fahrenheit
        return httpx.Response(200, json={"vid": 1, "fid": 1})
    if "/Patient/" in route:
        return httpx.Response(
            200,
            json={
                "resourceType": "Patient",
                "id": PATIENT,
                "name": [{"family": "SYNTHETIC", "given": ["FHIRBridgeDemo"]}],
                "birthDate": "1990-01-01",
            },
        )
    if route.endswith("/Encounter"):
        assert request.url.params["patient"] == PATIENT
        return httpx.Response(
            200,
            json=bundle(
                [
                    {
                        "resourceType": "Encounter",
                        "id": ENCOUNTER,
                        "subject": {"reference": "Patient/" + PATIENT},
                    }
                ]
            ),
        )
    if request.url.params.get("page") == "2":
        return httpx.Response(
            200,
            json=bundle(
                [
                    {
                        "resourceType": "Observation",
                        "id": "v2",
                        "subject": {"reference": "Patient/" + PATIENT},
                        "code": {"coding": [{"system": "http://loinc.org", "code": "8310-5"}]},
                        "valueQuantity": {"value": 36.8, "code": "Cel"},
                    }
                ]
            ),
        )
    return httpx.Response(
        200,
        json=bundle(
            [
                {
                    "resourceType": "Observation",
                    "id": "v1",
                    "subject": {"reference": "Patient/" + PATIENT},
                    "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                    "valueQuantity": {"value": 68, "code": "/min"},
                }
            ],
            "https://ehr.test/apis/default/fhir/Observation?page=2",
        ),
    )


async def test_openemr_native_seed_then_fhir_patient_encounter_observations():
    async with httpx.AsyncClient(
        base_url="https://ehr.test/apis/default/fhir/", transport=httpx.MockTransport(upstream)
    ) as http:
        result = await seed_demo(FhirClient(http, "private-token"))
        assert result["patient_id"] == PATIENT
        assert result["encounter_count"] == 1
        assert result["observation_count"] == 2
        assert result["observations"][1]["value"] == 36.8
        assert result["seeded_via"] == "OpenEMR standard API"
        assert result["read_via"] == "FHIR R4"


async def test_patient_read_refuses_non_demo_patient():
    async with httpx.AsyncClient(
        base_url="https://ehr.test/fhir/",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"resourceType": "Patient", "name": [{"family": "Real"}]}
            )
        ),
    ) as http:
        with pytest.raises(BridgeError, match="synthetic"):
            await patient_summary(FhirClient(http), PATIENT)


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.test/Observation",
        "https://ehr.test/oauth/token",
        "https://ehr.test/apis/default/fhir/Observation?loop=1",
    ],
)
async def test_pagination_cannot_leak_tokens_or_loop(next_url):
    calls = [0]

    def handler(request):
        calls[0] += 1
        if "/Patient/" in request.url.path:
            return upstream(request)
        return httpx.Response(200, json=bundle([], next_url))

    async with httpx.AsyncClient(
        base_url="https://ehr.test/apis/default/fhir/", transport=httpx.MockTransport(handler)
    ) as http:
        with pytest.raises(BridgeError):
            await patient_summary(FhirClient(http, "private-token"), PATIENT)
    assert calls[0] <= 4


async def test_openemr_validation_errors_are_not_silently_accepted():
    async with httpx.AsyncClient(
        base_url="https://ehr.test/apis/default/fhir/",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"validationErrors": {"private": "details"}})
        ),
    ) as http:
        with pytest.raises(BridgeError) as caught:
            await seed_demo(FhirClient(http, "private-token"))
        assert "private" not in str(caught.value)


async def test_openemr_collection_search_bundle_compatibility():
    # OpenEMR7.0.4 FhirResourcesService::createBundle labels searches 'collection'.
    def legacy(request):
        response = upstream(request)
        data = response.json()
        if data.get("resourceType") == "Bundle":
            data["type"] = "collection"
        return httpx.Response(response.status_code, json=data)

    async with httpx.AsyncClient(
        base_url="https://ehr.test/apis/default/fhir/", transport=httpx.MockTransport(legacy)
    ) as http:
        summary = await patient_summary(FhirClient(http, "private-token"), PATIENT)
        assert summary["encounter_count"] == 1
        assert summary["observation_count"] == 2


@pytest.mark.parametrize("case", ["mixed", "zero", "empty"])
async def test_summary_distinguishes_measurements_from_missing_data(case):
    absent_reason = {
        "coding": [
            {
                "system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                "code": "unknown",
            }
        ]
    }
    items = []
    if case == "mixed":
        items = [
            {"id": "pulse", "valueQuantity": {"value": 68, "code": "/min"}},
            {"id": "weight", "valueQuantity": {"value": 159.83514, "code": "[lb_av]"}},
            {"id": "temperature", "valueQuantity": {"value": 98.24, "code": "[degF]"}},
            *[{"id": f"missing-{i}", "dataAbsentReason": absent_reason} for i in range(10)],
            {"id": "panel", "hasMember": [{"reference": "Observation/pulse"}]},
            {"id": "site", "valueCodeableConcept": {"text": "oral"}},
        ]
    elif case == "zero":
        items = [{"id": "zero", "valueQuantity": {"value": 0, "code": "kg"}}]

    def handler(request):
        if not request.url.path.endswith("/Observation"):
            return upstream(request)
        return httpx.Response(
            200,
            json=bundle(
                [
                    {
                        "resourceType": "Observation",
                        "subject": {"reference": "Patient/" + PATIENT},
                        **item,
                    }
                    for item in items
                ]
            ),
        )

    async with httpx.AsyncClient(
        base_url="https://ehr.test/apis/default/fhir/", transport=httpx.MockTransport(handler)
    ) as http:
        summary = await patient_summary(FhirClient(http, "private-token"), PATIENT)
    assert summary["observation_count"] == len(items)
    assert summary["numeric_observation_count"] == {"mixed": 3, "zero": 1, "empty": 0}[case]
    assert summary["data_absent_observation_count"] == (10 if case == "mixed" else 0)
    if case == "mixed":
        assert summary["observations"][3]["data_absent_reason"] == absent_reason
        assert summary["observations"][13]["data_absent_reason"] is None
