# Visit reports

I use this report to inspect the relationships and measurement details in a fictional record transfer. It groups what the record system actually linked together. It does not reconstruct a clinical history from timestamps.

## Try it

Follow the Python setup in the [README](../README.md), then run:

```sh
uv run python scripts/smoke_hapi.py
```

The script creates the same five fictional resources twice and checks the read-back report. It expects one visit with three assigned observations, no unassigned observations and no excluded observations. It deletes its five resources in a `finally` block, including on failure. No listening server or browser is started.

For the local OpenEMR setup, `uv run python scripts/smoke_openemr.py` requests both the original patient summary and `GET /openemr/patients/{patient_id}/visits`. It checks that the report accounts for all 15 entries, preserves the three original quantities and ten absent-data reasons, and contains every returned visit. The [integration guide](integration.md) explains interactive sign-in and cleanup. The route accepts only the fixed fictional patient's identity and birth date. A configured SMART client requires a signed-in session just like the original patient summary.

## Output

The HAPI `GET /demo/{run_id}` response includes a `visit_report`. The OpenEMR visits route returns that same report shape directly:

```json
{
  "patient_id": "fictional-patient-id",
  "synthetic_only": true,
  "read_via": "FHIR R4",
  "counts": {"total": 3, "assigned": 3, "unassigned": 0, "excluded": 0},
  "visits": [
    {"id": "fictional-visit-id", "status": "finished", "observations": ["..."]}
  ],
  "unassigned_observations": [],
  "excluded_observations": []
}
```

This is an abbreviated shape, not an input fixture. Real observations are objects with `id`, `status`, coded measurement names and their values. The report preserves quantity fields including the unit system, unit code and any comparator such as "less than". Coded and text results are retained too. Blood-pressure `component` entries retain their individual codes, quantities and absence reasons. `effective[x]` is when the result applies; `issued` is when it became available. Neither determines visit membership.

`counts.total` counts observation resources, not numeric values or blood-pressure components. It always equals `assigned + unassigned + excluded` in a successful report. Visits remain in source order, including empty visits. Results are not ranked, averaged or deduplicated by measurement code. Corrected and amended records retain their status and ID; the report does not claim to select the latest clinical truth.

## Link and status rules

- `Observation.encounter` must identify a returned Encounter. A relative `Encounter/id` reference or an absolute reference under the configured FHIR base is supported. Matching dates or display names are insufficient.
- A link to an encounter not returned by the search is marked `encounter-not-returned`. Missing links, foreign-server links, contained references, UUID references and version-specific references are marked `missing-or-unresolved-encounter`. The report never fetches an arbitrary link or drops a version suffix to guess at a current record.
- Every Encounter and Observation must have a distinct resource ID and an explicit subject matching the patient. Duplicate IDs, missing IDs or mismatched patients fail the whole report with a bounded upstream error. This avoids double counting repeated search pages or mixing patients.
- Observations with `entered-in-error` or `cancelled` status go to `excluded_observations`, with their values retained for inspection. Results linked to an encounter marked `entered-in-error` are excluded too.
- Unrecognized or missing observation statuses, conflicting value/absence fields, or unsupported modifier extensions go to `unassigned_observations` with a reason. A modifier extension is extra information that can change the meaning of a record. I retain it for review instead of ignoring it.
- Preliminary, registered, final, amended, corrected and unknown statuses remain explicit. Inclusion in a visit is a relationship, not a claim that a result is final or valid for treatment.

The implementation is an explicit projection of a supported subset, not a full FHIR profile validator or a clinical decision tool. It preserves the displayed values and contextual fields; callers should keep the original records for complete source history. It does not resolve panel members, infer absent-data reasons, convert units, reconcile record versions or support real patient data.

## Files and checks

`src/fhir_emr_bridge/reports.py` builds the report without network calls. `openemr.py` first verifies the fictional patient and loads patient-scoped, bounded paginated searches. `fhir.py` uses the same report builder after verifying the five HAPI records. `app.py` exposes the OpenEMR route using the existing server-side authorization.

`tests/test_reports.py` contains 30 cases for report semantics. `tests/test_visit_api.py` adds six cases for authorization, identity checks, pagination and report responses. The existing HAPI route test checks the report too. Both smoke scripts assert report contents against actual server reads; CI runs the real OpenEMR smoke check.

I followed the HL7 R4 definitions of [Observation](https://hl7.org/fhir/R4/observation.html), [resource references](https://hl7.org/fhir/R4/references.html) and [Encounter](https://hl7.org/fhir/R4/encounter.html). In particular, an observation's subject and encounter are separate references, and `dataAbsentReason` explains a missing value rather than supplying a numeric zero.
