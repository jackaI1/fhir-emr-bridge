"""Explicit OpenEMR native-write/FHIR-read adapter for the disposable local demo."""

from urllib.parse import urljoin, urlsplit
from uuid import UUID

from .fhir import BridgeError, FhirClient, request_json
from .reports import ReportError, build_visit_report


async def search(client: FhirClient, kind: str, patient_id: str) -> list[dict]:
    base = str(client.http.base_url)
    url = base + kind + "?patient=" + patient_id + "&_count=100"
    visited = set()
    found = []
    for _ in range(10):
        if url in visited or not url.startswith(base):
            raise BridgeError("Unsafe or repeated FHIR pagination link")
        visited.add(url)
        page = await client.request("GET", url)
        # OpenEMR7.0.4 labels search results collection (FhirResourcesService).
        if page.get("resourceType") != "Bundle" or page.get("type") not in {
            "searchset",
            "collection",
        }:
            raise BridgeError("Expected a FHIR search Bundle")
        for entry in page.get("entry", []):
            resource = entry.get("resource", {})
            if entry.get("search", {}).get("mode") == "outcome":
                raise BridgeError("FHIR search returned an OperationOutcome")
            if resource.get("resourceType") != kind:
                raise BridgeError("Unexpected resource in FHIR search")
            subject = resource.get("subject", {}).get("reference", "")
            if subject not in {"Patient/" + patient_id, base + "Patient/" + patient_id}:
                raise BridgeError("FHIR search returned a different patient")
            found.append(resource)
        next_links = [
            link.get("url") for link in page.get("link", []) if link.get("relation") == "next"
        ]
        if not next_links:
            return found
        if len(next_links) != 1 or not isinstance(next_links[0], str):
            raise BridgeError("Invalid FHIR pagination link")
        url = urljoin(url, next_links[0])
        parsed = urlsplit(url)
        # Prefix alone is insufficient: reject dot paths, userinfo and fragments as well.
        if parsed.fragment or parsed.username or "/.." in parsed.path:
            raise BridgeError("Unsafe FHIR pagination link")
    raise BridgeError("FHIR pagination limit exceeded; refusing a truncated summary")


async def patient_records(client: FhirClient, patient_id: str) -> tuple[list[dict], list[dict]]:
    patient = await client.request("GET", "Patient/" + patient_id)
    names = patient.get("name", [])
    if (
        patient.get("resourceType") != "Patient"
        or patient.get("id") != patient_id
        or patient.get("birthDate") != "1990-01-01"
        or not any(
            name.get("family") == "SYNTHETIC" and name.get("given") == ["FHIRBridgeDemo"]
            for name in names
        )
    ):
        raise BridgeError(
            "This endpoint only reads the fixed synthetic OpenEMR demo patient", status=409
        )
    encounters = await search(client, "Encounter", patient_id)
    observations = await search(client, "Observation", patient_id)
    return encounters, observations


async def patient_visits(client: FhirClient, patient_id: str) -> dict:
    encounters, observations = await patient_records(client, patient_id)
    try:
        return build_visit_report(
            patient_id, encounters, observations, base_url=str(client.http.base_url)
        )
    except ReportError as exc:
        raise BridgeError(str(exc)) from exc


async def patient_summary(client: FhirClient, patient_id: str) -> dict:
    encounters, observations = await patient_records(client, patient_id)
    vitals = []
    for obs in observations:
        quantity = obs.get("valueQuantity", {})
        coding = next(
            (
                c
                for c in obs.get("code", {}).get("coding", [])
                if c.get("system") == "http://loinc.org"
            ),
            {},
        )
        vitals.append(
            {
                "id": obs.get("id"),
                "code": coding.get("code"),
                "value": quantity.get("value"),
                "unit": quantity.get("code"),
                "data_absent_reason": obs.get("dataAbsentReason"),
            }
        )
    return {
        "patient_id": patient_id,
        "synthetic_only": True,
        "read_via": "FHIR R4",
        "encounter_count": len(encounters),
        "observation_count": len(observations),
        "numeric_observation_count": sum(
            isinstance(vital["value"], (int, float)) and not isinstance(vital["value"], bool)
            for vital in vitals
        ),
        "data_absent_observation_count": sum(bool(vital["data_absent_reason"]) for vital in vitals),
        "observations": vitals,
    }


async def seed_demo(client: FhirClient) -> dict:
    """Each call creates a new fictional patient. Reset via compose down -v.

    OpenEMR 7.0.4 does NOT expose Patient/Encounter/Observation FHIR writes.
    Seed with its standard API using the same SMART bearer token, then read FHIR.
    """
    base = str(client.http.base_url)
    if not base.endswith("/fhir/"):
        raise BridgeError("OpenEMR demo requires a /fhir base URL", status=409)
    standard = base.removesuffix("fhir/") + "api/"
    headers = {**client.headers, "Content-Type": "application/json", "Accept": "application/json"}

    async def post(route, data, *, vital=False):
        result = await request_json(
            client.http, "POST", standard + route, headers=headers, json=data
        )
        if vital and all(isinstance(result.get(k), int) for k in ["vid", "fid"]):
            return result
        if (
            result.get("validationErrors")
            or result.get("internalErrors")
            or result.get("error")
            or not isinstance(result.get("data"), dict)
        ):
            raise BridgeError(
                "OpenEMR rejected synthetic fixture; check API scopes and permissions"
            )
        return result["data"]

    try:
        patient = await post(
            "patient",
            {
                "fname": "FHIRBridgeDemo",
                "lname": "SYNTHETIC",
                "DOB": "1990-01-01",
                "sex": "Male",
                "pubpid": "FHIR-BRIDGE-SYNTHETIC",
            },
        )
        puuid = str(UUID(patient["uuid"]))
        pid = int(patient["pid"])
        encounter = await post(
            f"patient/{puuid}/encounter",
            {
                "date": "2025-01-15 10:00:00",
                "reason": "Synthetic integration demonstration",
                "pc_catid": "5",
                "class_code": "AMB",
                "facility_id": "1",
                "billing_facility": "1",
                "provider_id": "1",
            },
        )
        eid = int(encounter["encounter"])
        # Avoid the 7.0.4 BP-mean subscriber, which assumes a browser authUserID
        # during API writes. The integration needs weight, pulse and temperature only.
        await post(
            f"patient/{pid}/encounter/{eid}/vital",
            {
                "date": "2025-01-15 10:05:00",
                "weight": "159.83514",
                "temperature": "98.24",
                "temp_method": "oral",
                "pulse": "68",
                "note": "SYNTHETIC ONLY - FHIR bridge integration demonstration",
            },
            vital=True,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise BridgeError("Unexpected OpenEMR fixture response") from exc
    result = await patient_summary(client, puuid)
    result["seeded_via"] = "OpenEMR standard API"
    return result
