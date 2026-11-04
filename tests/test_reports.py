"""Fictional records exercise the visit-link semantics independently of HTTP."""

from copy import deepcopy
from uuid import UUID

import pytest

from fhir_emr_bridge.reports import ReportError, build_visit_report
from fhir_emr_bridge.synthetic import resources

BASE = "https://ehr.test/fhir/"
PATIENT, ENCOUNTER, *OBSERVATIONS = resources(UUID("ad0eb8de-b9ad-4ab8-9735-316f612a0183"))
ABSENT = {
    "coding": [
        {
            "system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
            "code": "not-performed",
        }
    ]
}


def report(encounters=None, observations=None):
    return build_visit_report(
        PATIENT["id"],
        deepcopy([ENCOUNTER] if encounters is None else encounters),
        deepcopy(OBSERVATIONS if observations is None else observations),
        base_url=BASE,
    )


def test_two_visits_at_same_time_follow_explicit_links_instead_of_timestamps():
    second = {**deepcopy(ENCOUNTER), "id": "second-visit"}
    observations = deepcopy(OBSERVATIONS)
    observations[1]["encounter"] = {"reference": BASE + "Encounter/second-visit"}
    result = report([ENCOUNTER, second], observations)
    assert result["counts"] == {"total": 3, "assigned": 3, "unassigned": 0, "excluded": 0}
    assert [o["id"] for o in result["visits"][0]["observations"]] == [
        OBSERVATIONS[0]["id"],
        OBSERVATIONS[2]["id"],
    ]
    assert result["visits"][1]["observations"][0]["valueQuantity"]["value"] == 68
    assert result["visits"][0]["period"] == result["visits"][1]["period"]


def test_blood_pressure_components_missing_data_and_non_numeric_results_are_preserved():
    bp = deepcopy(OBSERVATIONS[0])
    del bp["valueQuantity"]
    bp["code"] = {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]}
    bp["component"] = [
        {
            "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
            "valueQuantity": {
                "value": 120,
                "comparator": "<",
                "system": "http://unitsofmeasure.org",
                "unit": "mmHg",
                "code": "mm[Hg]",
            },
        },
        {
            "code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
            "dataAbsentReason": ABSENT,
        },
    ]
    bp["status"] = "preliminary"
    observations = [bp, deepcopy(OBSERVATIONS[1]), deepcopy(OBSERVATIONS[2])]
    observations[1].pop("valueQuantity")
    observations[1]["dataAbsentReason"] = ABSENT
    observations[2].pop("valueQuantity")
    observations[2]["valueCodeableConcept"] = {"text": "oral"}
    result = report(observations=observations)
    views = result["visits"][0]["observations"]
    assert views[0]["component"] == bp["component"]
    assert views[0]["status"] == "preliminary"
    assert "valueQuantity" not in views[1]
    assert views[1]["dataAbsentReason"] == ABSENT
    assert views[2]["valueCodeableConcept"] == {"text": "oral"}


def test_zero_and_imperial_units_are_neither_removed_nor_converted():
    observations = deepcopy(OBSERVATIONS)
    observations[0]["valueQuantity"] = {
        "value": 0,
        "system": "http://unitsofmeasure.org",
        "code": "[lb_av]",
        "unit": "pounds",
    }
    view = report(observations=observations)["visits"][0]["observations"][0]
    assert view["valueQuantity"] == observations[0]["valueQuantity"]


@pytest.mark.parametrize(
    "reference",
    [
        None,
        {"display": "same visit"},
        {"reference": "Encounter/not-returned"},
        {"reference": "https://elsewhere.test/fhir/Encounter/" + ENCOUNTER["id"]},
        {"reference": "Encounter/" + ENCOUNTER["id"] + "/_history/1"},
        {"reference": "#contained-visit"},
        {"reference": "urn:uuid:unresolved"},
        {"reference": BASE + "Encounter/../Encounter/" + ENCOUNTER["id"]},
    ],
)
def test_unresolved_references_are_visible_and_never_guessed(reference):
    observation = deepcopy(OBSERVATIONS[0])
    if reference is None:
        observation.pop("encounter")
    else:
        observation["encounter"] = reference
    result = report(observations=[observation])
    assert result["counts"] == {"total": 1, "assigned": 0, "unassigned": 1, "excluded": 0}
    assert result["unassigned_observations"][0]["observation"]["id"] == observation["id"]


@pytest.mark.parametrize("status", ["entered-in-error", "cancelled"])
def test_excluded_observations_remain_auditable_without_appearing_as_visit_results(status):
    observation = {**deepcopy(OBSERVATIONS[0]), "status": status}
    result = report(observations=[observation])
    assert result["counts"]["excluded"] == 1
    assert result["visits"][0]["observations"] == []
    assert result["excluded_observations"][0]["reason"] == status
    assert result["excluded_observations"][0]["observation"]["valueQuantity"]["value"] == 72.5


def test_erroneous_encounter_is_not_used_for_active_results():
    result = report(encounters=[{**ENCOUNTER, "status": "entered-in-error"}])
    assert result["counts"]["excluded"] == 3
    assert result["excluded_observations"][0]["reason"] == "encounter-entered-in-error"


@pytest.mark.parametrize("kind", ["Encounter", "Observation"])
@pytest.mark.parametrize(
    "mutation", ["wrong-patient", "foreign-patient", "duplicate", "missing-id"]
)
def test_mixed_patients_and_ambiguous_identities_fail_the_entire_report(kind, mutation):
    encounters, observations = deepcopy([ENCOUNTER]), deepcopy(OBSERVATIONS)
    records = encounters if kind == "Encounter" else observations
    if mutation == "wrong-patient":
        records[0]["subject"] = {"reference": "Patient/another"}
    elif mutation == "foreign-patient":
        records[0]["subject"] = {"reference": "https://other.test/Patient/" + PATIENT["id"]}
    elif mutation == "duplicate":
        records.append(deepcopy(records[0]))
    else:
        records[0].pop("id")
    with pytest.raises(ReportError):
        report(encounters, observations)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("modifier", "unsupported-modifier-extension"),
        ("encounter-modifier", "unsupported-encounter-modifier-extension"),
        ("missing-status", "unrecognized-or-missing-status"),
        ("conflicting-absence", "conflicting-value-fields"),
        ("two-values", "conflicting-value-fields"),
        ("component-conflict", "conflicting-value-fields"),
    ],
)
def test_unsupported_or_conflicting_meanings_stay_visible_for_review(mutation, reason):
    encounters, observations = deepcopy([ENCOUNTER]), deepcopy(OBSERVATIONS[:1])
    if mutation == "modifier":
        observations[0]["modifierExtension"] = [{"url": "https://example.invalid/modifier"}]
    elif mutation == "encounter-modifier":
        encounters[0]["modifierExtension"] = [{"url": "https://example.invalid/modifier"}]
    elif mutation == "missing-status":
        observations[0].pop("status")
    elif mutation == "conflicting-absence":
        observations[0]["dataAbsentReason"] = ABSENT
    elif mutation == "two-values":
        observations[0]["valueString"] = "not another measurement"
    else:
        observations[0]["component"] = [{"valueQuantity": {"value": 1}, "dataAbsentReason": ABSENT}]
    result = report(encounters, observations)
    assert result["counts"]["unassigned"] == 1
    assert result["unassigned_observations"][0]["reason"] == reason


def test_projection_is_detached_and_empty_visits_are_retained():
    assert report(observations=[])["visits"][0]["observations"] == []
    observations = deepcopy(OBSERVATIONS)
    result = build_visit_report(PATIENT["id"], [ENCOUNTER], observations, base_url=BASE)
    result["visits"][0]["observations"][0]["valueQuantity"]["value"] = 999
    assert observations[0]["valueQuantity"]["value"] == 72.5
    assert sum(result["counts"][k] for k in ("assigned", "unassigned", "excluded")) == 3


def test_malformed_components_fail_with_a_bounded_error():
    observation = {**deepcopy(OBSERVATIONS[0]), "component": "not a component list"}
    with pytest.raises(ReportError, match="Malformed Observation components"):
        report(observations=[observation])
