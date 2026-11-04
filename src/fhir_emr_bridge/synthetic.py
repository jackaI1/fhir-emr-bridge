"""Fixed, obviously fictional R4 resources; callers cannot supply clinical data."""

from uuid import UUID

TAG = {"system": "https://github.com/jackaI1/fhir-emr-bridge", "code": "synthetic"}


def resources(run_id: UUID) -> list[dict]:
    suffix = run_id.hex
    patient_id, encounter_id = f"feb-p-{suffix}", f"feb-e-{suffix}"
    patient_ref = {"reference": f"Patient/{patient_id}"}
    encounter_ref = {"reference": f"Encounter/{encounter_id}"}
    patient = {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": [{"system": TAG["system"] + "/runs", "value": str(run_id)}],
        "active": True,
        "name": [{"use": "official", "family": "SYNTHETIC", "given": ["Demo"]}],
        "gender": "unknown",
        "birthDate": "1990-01-01",
    }
    encounter = {
        "resourceType": "Encounter",
        "id": encounter_id,
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": patient_ref,
        "period": {"start": "2025-01-15T10:00:00Z", "end": "2025-01-15T10:15:00Z"},
    }
    observations = []
    for key, code, label, value, unit in [
        ("weight", "29463-7", "Body weight", 72.5, "kg"),
        ("pulse", "8867-4", "Heart rate", 68, "/min"),
        ("temperature", "8310-5", "Body temperature", 36.8, "Cel"),
    ]:
        observations.append(
            {
                "resourceType": "Observation",
                "id": f"feb-{key}-{suffix}",
                "status": "final",
                "category": [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                "code": "vital-signs",
                            }
                        ]
                    }
                ],
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": code, "display": label}]
                },
                "subject": patient_ref,
                "encounter": encounter_ref,
                "effectiveDateTime": "2025-01-15T10:05:00Z",
                "valueQuantity": {
                    "value": value,
                    "unit": unit,
                    "system": "http://unitsofmeasure.org",
                    "code": unit,
                },
            }
        )
    result = [patient, encounter, *observations]
    for resource in result:
        resource["meta"] = {"tag": [dict(TAG)]}
    return result
