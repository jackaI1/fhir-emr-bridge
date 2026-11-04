"""FHIR transport and repeatable synthetic integration checks."""

from uuid import UUID

import httpx

from .synthetic import TAG, resources


class BridgeError(Exception):
    def __init__(self, message: str, *, status: int = 502, completed: int = 0):
        super().__init__(message)
        self.status = status
        self.completed = completed


async def request_json(http: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    """Never relay upstream response bodies, URLs, or credentials in errors."""
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise BridgeError("Upstream unavailable; check connectivity and TLS") from exc
    if response.status_code == 404:
        raise BridgeError("Upstream resource not found (404)", status=404)
    if not response.is_success:
        raise BridgeError(f"Upstream returned HTTP {response.status_code}")
    if response.status_code == 204 or not response.content:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise BridgeError("Upstream returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise BridgeError("Upstream returned a non-object JSON response")
    return data


def path(resource: dict) -> str:
    return f"{resource['resourceType']}/{resource['id']}"


def owned(resource: dict) -> bool:
    meta = resource.get("meta")
    tags = meta.get("tag") if isinstance(meta, dict) else None
    return isinstance(tags, list) and any(contains(tag, TAG) for tag in tags)


def contains(actual, expected) -> bool:
    """Allow server-added fields, require every supplied clinical value and reference."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and contains(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(
            any(contains(candidate, item) for candidate in actual) for item in expected
        )
    return actual == expected


class FhirClient:
    def __init__(self, http: httpx.AsyncClient, token: str | None = None):
        self.http = http
        self.headers = {"Accept": "application/fhir+json", "Content-Type": "application/fhir+json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def request(self, method: str, url: str, **kwargs) -> dict:
        return await request_json(self.http, method, url, headers=self.headers, **kwargs)

    async def capabilities(self) -> dict:
        data = await self.request("GET", "metadata")
        if data.get("resourceType") != "CapabilityStatement" or data.get("fhirVersion") != "4.0.1":
            raise BridgeError("This integration requires a FHIR R4 (4.0.1) CapabilityStatement")
        rest = data.get("rest", [])
        if not isinstance(rest, list):
            raise BridgeError("Malformed FHIR capabilities")
        for entry in rest:
            if not isinstance(entry, dict) or not isinstance(entry.get("resource", []), list):
                raise BridgeError("Malformed FHIR capabilities")
            for resource in entry.get("resource", []):
                if (
                    not isinstance(resource, dict)
                    or not isinstance(resource.get("type"), str)
                    or not isinstance(resource.get("interaction", []), list)
                ):
                    raise BridgeError("Malformed FHIR resource capabilities")
                for interaction in resource.get("interaction", []):
                    if not isinstance(interaction, dict) or not isinstance(
                        interaction.get("code"), str
                    ):
                        raise BridgeError("Malformed FHIR interaction capabilities")
                for flag in ["updateCreate", "conditionalUpdate"]:
                    if flag in resource and not isinstance(resource[flag], bool):
                        raise BridgeError("Malformed FHIR update capabilities")
        return data

    async def require(self, operation: str) -> dict:
        metadata = await self.capabilities()
        supported = {
            resource["type"]: resource
            for rest in metadata.get("rest", [])
            if rest.get("mode") == "server"
            for resource in rest.get("resource", [])
            if "type" in resource
        }
        for kind in ["Patient", "Encounter", "Observation"]:
            resource = supported.get(kind, {})
            codes = {op.get("code") for op in resource.get("interaction", [])}
            if (
                operation not in codes
                or "read" not in codes
                or (
                    operation == "update"
                    and (
                        resource.get("updateCreate") is False
                        or not (resource.get("updateCreate") or resource.get("conditionalUpdate"))
                    )
                )
            ):
                raise BridgeError(
                    f"{kind} does not advertise the required {operation} support; "
                    "use HAPI for the write demonstration",
                    status=409,
                )

        return supported

    async def existing(self, resource: dict) -> dict | None:
        try:
            return await self.request("GET", path(resource))
        except BridgeError as exc:
            if exc.status == 404:
                return None
            raise

    async def check_ownership(self, expected: list[dict]) -> list[dict]:
        found = []
        for resource in expected:
            actual = await self.existing(resource)
            if actual is not None:
                if not owned(actual):
                    raise BridgeError(
                        "Refusing to modify a resource without the synthetic tag", status=409
                    )
                found.append(resource)
        return found

    async def write_run(self, run_id: UUID) -> dict:
        supported = await self.require("update")
        expected = resources(run_id)
        await self.check_ownership(expected)
        completed = 0
        try:
            for resource in expected:
                kind = resource["resourceType"]
                target = path(resource) if supported[kind].get("updateCreate") else kind
                params = None if target != kind else {"_id": resource["id"]}
                await self.request("PUT", target, params=params, json=resource)
                completed += 1
            return await self.read_run(run_id)
        except BridgeError as exc:
            raise BridgeError(
                f"Run incomplete; retry the same run UUID. {exc}",
                status=exc.status,
                completed=completed,
            ) from exc

    async def read_run(self, run_id: UUID) -> dict:
        actual_resources = []
        for expected in resources(run_id):
            actual = await self.request("GET", path(expected))
            if not contains(actual, expected):
                raise BridgeError(
                    f"Synthetic verification failed for {expected['resourceType']}", status=409
                )
            actual_resources.append(actual)
        return {
            "run_id": str(run_id),
            "verified": True,
            "resource_count": len(actual_resources),
            "patient_id": actual_resources[0]["id"],
            "encounter_id": actual_resources[1]["id"],
            "observations": [
                {
                    "id": obs["id"],
                    "code": obs["code"]["coding"][0]["code"],
                    "value": obs["valueQuantity"]["value"],
                    "unit": obs["valueQuantity"]["code"],
                }
                for obs in actual_resources[2:]
            ],
        }

    async def delete_run(self, run_id: UUID) -> dict:
        await self.require("delete")
        found = await self.check_ownership(resources(run_id))
        completed = 0
        try:
            for resource in reversed(found):
                await self.request("DELETE", path(resource))
                completed += 1
        except BridgeError as exc:
            raise BridgeError(
                f"Cleanup incomplete; retry the same run UUID. {exc}",
                status=exc.status,
                completed=completed,
            ) from exc
        return {"run_id": str(run_id), "deleted": completed}
