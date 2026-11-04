"""Visit-level projections of R4 records, without inferring clinical meaning."""

import re
from copy import deepcopy

STATUSES = {"registered", "preliminary", "final", "amended", "corrected", "unknown"}


class ReportError(ValueError):
    """An upstream result cannot be reported without mixing or duplicating records."""


def reference_id(reference: dict, kind: str, base_url: str) -> str | None:
    """Resolve only unversioned relative or same-base literal references; never fetch URLs."""
    value = reference.get("reference") if isinstance(reference, dict) else None
    if not isinstance(value, str):
        return None
    value = value.removeprefix(base_url.rstrip("/") + "/")
    match = re.fullmatch(re.escape(kind) + r"/([A-Za-z0-9.-]{1,64})", value)
    return match[1] if match else None


def observation_view(resource: dict) -> dict:
    """Keep coded values, comparators, components and absence reasons exactly as supplied."""
    fields = {
        "id",
        "status",
        "code",
        "encounter",
        "effectiveDateTime",
        "effectivePeriod",
        "issued",
        "dataAbsentReason",
        "component",
        "hasMember",
        "derivedFrom",
        "modifierExtension",
        "interpretation",
        "referenceRange",
        "method",
        "bodySite",
        "specimen",
        "device",
    }
    return deepcopy(
        {k: v for k, v in resource.items() if k in fields or k.startswith(("value", "effective"))}
    )


def review_reason(observation: dict) -> str | None:
    if observation.get("status") not in STATUSES:
        return "unrecognized-or-missing-status"
    # These fields can change interpretation. Preserve them for review rather than ignore them.
    components = observation.get("component", [])
    if not isinstance(components, list) or not all(isinstance(c, dict) for c in components):
        raise ReportError("Malformed Observation components in visit report")
    for item in [observation, *components]:
        if item.get("modifierExtension"):
            return "unsupported-modifier-extension"
        values = [key for key in item if key.startswith("value")]
        if len(values) > 1 or (values and item.get("dataAbsentReason")):
            return "conflicting-value-fields"
    return None


def build_visit_report(
    patient_id: str, encounters: list[dict], observations: list[dict], *, base_url: str
) -> dict:
    """Group by Observation.encounter, never by dates or list position.

    Callers enforce the synthetic-patient boundary before supplying records. This is a
    projection, not a full FHIR validator, unit converter or clinical interpretation.
    """
    visits = {}
    for kind, records in (("Encounter", encounters), ("Observation", observations)):
        seen = set()
        for resource in records:
            if not isinstance(resource, dict):
                raise ReportError("Malformed resource in visit report")
            identifier = resource.get("id")
            if (
                resource.get("resourceType") != kind
                or not isinstance(identifier, str)
                or not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", identifier)
            ):
                raise ReportError("Missing or invalid resource identity in visit report")
            if identifier in seen:
                raise ReportError("Duplicate resource identity in visit report")
            seen.add(identifier)
            if reference_id(resource.get("subject"), "Patient", base_url) != patient_id:
                raise ReportError("Visit report received a different or unresolved patient")

    encounter_by_id = {e["id"]: e for e in encounters}
    for encounter in encounters:
        visits[encounter["id"]] = {
            **deepcopy(
                {
                    k: encounter[k]
                    for k in ("id", "status", "period", "modifierExtension")
                    if k in encounter
                }
            ),
            "observations": [],
        }

    unassigned, excluded = [], []
    for observation in observations:
        view = observation_view(observation)
        status = observation.get("status")
        encounter_id = reference_id(observation.get("encounter"), "Encounter", base_url)
        visit = visits.get(encounter_id)
        if status in {"entered-in-error", "cancelled"}:
            excluded.append({"reason": status, "observation": view})
        elif visit and visit.get("status") == "entered-in-error":
            excluded.append({"reason": "encounter-entered-in-error", "observation": view})
        else:
            reason = review_reason(observation)
            if not reason and not visit:
                reason = (
                    "encounter-not-returned" if encounter_id else "missing-or-unresolved-encounter"
                )
            if not reason:
                encounter = encounter_by_id[encounter_id]
                if encounter.get("modifierExtension"):
                    reason = "unsupported-encounter-modifier-extension"
            if reason:
                unassigned.append({"reason": reason, "observation": view})
            else:
                visit["observations"].append(view)

    assigned = sum(len(v["observations"]) for v in visits.values())
    return {
        "patient_id": patient_id,
        "synthetic_only": True,
        "read_via": "FHIR R4",
        "counts": {
            "total": len(observations),
            "assigned": assigned,
            "unassigned": len(unassigned),
            "excluded": len(excluded),
        },
        "visits": list(visits.values()),
        "unassigned_observations": unassigned,
        "excluded_observations": excluded,
    }
