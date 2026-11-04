import copy
import json
from uuid import UUID

import httpx
import pytest

from fhir_emr_bridge.fhir import BridgeError, FhirClient
from fhir_emr_bridge.synthetic import resources

RUN = UUID("ad0eb8de-b9ad-4ab8-9735-316f612a0183")


def capability(write=True):
    return {
        "resourceType": "CapabilityStatement",
        "fhirVersion": "4.0.1",
        "rest": [
            {
                "mode": "server",
                "resource": [
                    {
                        "type": kind,
                        "updateCreate": write,
                        "interaction": [
                            {"code": op} for op in ["read", "search-type", "update", "delete"]
                        ],
                    }
                    for kind in ["Patient", "Encounter", "Observation"]
                ],
            }
        ],
    }


class FhirServer:
    """HTTP boundary fixture: no real server, port, or background worker."""

    def __init__(self):
        self.data = {}
        self.capability = capability()
        self.writes = 0
        self.fail_at = None

    def __call__(self, request):
        path = request.url.path.removeprefix("/fhir/").strip("/")
        if path == "metadata":
            return httpx.Response(200, json=self.capability)
        if request.method == "PUT":
            self.writes += 1
            if self.fail_at == self.writes:
                return httpx.Response(503, text="private clinical details and token")
            body = json.loads(request.content)
            assert body["resourceType"] + "/" + body["id"] == path
            assert request.headers["Content-Type"] == "application/fhir+json"
            self.data[path] = body
            return httpx.Response(201, json=body)
        if request.method == "DELETE":
            self.data.pop(path, None)
            return httpx.Response(204)
        if path not in self.data:
            return httpx.Response(404, json={"resourceType": "OperationOutcome"})
        return httpx.Response(200, json=self.data[path])


@pytest.fixture
async def server_client():
    server = FhirServer()
    async with httpx.AsyncClient(
        base_url="https://fhir.test/fhir/", transport=httpx.MockTransport(server)
    ) as http:
        yield server, FhirClient(http)


def test_fixed_resources_have_r4_links_and_coded_vitals():
    patient, encounter, *observations = resources(RUN)
    assert patient["name"][0]["family"] == "SYNTHETIC"
    assert patient["birthDate"] == "1990-01-01"
    assert encounter["class"]["code"] == "AMB"
    assert encounter["subject"]["reference"] == f"Patient/{patient['id']}"
    assert len(observations) == 3
    assert [
        (o["code"]["coding"][0]["code"], o["valueQuantity"]["value"], o["valueQuantity"]["code"])
        for o in observations
    ] == [("29463-7", 72.5, "kg"), ("8867-4", 68, "/min"), ("8310-5", 36.8, "Cel")]
    for observation in observations:
        assert observation["subject"]["reference"] == f"Patient/{patient['id']}"
        assert observation["encounter"]["reference"] == f"Encounter/{encounter['id']}"
        assert observation["valueQuantity"]["system"] == "http://unitsofmeasure.org"
        assert observation["meta"]["tag"][0]["code"] == "synthetic"


async def test_write_read_repeat_and_delete(server_client):
    server, client = server_client
    report = await client.write_run(RUN)
    assert report["verified"] is True
    assert report["resource_count"] == 5
    assert report["patient_id"] == "feb-p-ad0eb8deb9ad4ab89735316f612a0183"
    assert len(server.data) == 5
    await client.write_run(RUN)
    assert len(server.data) == 5
    assert (await client.read_run(RUN))["observations"][0]["value"] == 72.5
    assert (await client.delete_run(RUN))["deleted"] == 5
    assert server.data == {}
    assert (await client.delete_run(RUN))["deleted"] == 0


@pytest.mark.parametrize("mutation", ["version", "updates", "type"])
async def test_unsupported_server_prevents_all_writes(server_client, mutation):
    server, client = server_client
    if mutation == "version":
        server.capability["fhirVersion"] = "5.0.0"
    elif mutation == "type":
        server.capability["resourceType"] = "OperationOutcome"
    else:
        server.capability = capability(write=False)
    with pytest.raises(BridgeError):
        await client.write_run(RUN)
    assert server.writes == 0


async def test_partial_write_is_explicit_and_retryable(server_client):
    server, client = server_client
    server.fail_at = 3
    with pytest.raises(BridgeError) as caught:
        await client.write_run(RUN)
    assert caught.value.completed == 2
    assert "private" not in str(caught.value)
    assert "503" in str(caught.value)
    server.fail_at = None
    assert (await client.write_run(RUN))["verified"] is True
    assert len(server.data) == 5


@pytest.mark.parametrize("mutation", ["tag", "reference", "quantity", "code", "patient"])
async def test_verification_detects_corruption(server_client, mutation):
    server, client = server_client
    await client.write_run(RUN)
    data = list(server.data.values())
    if mutation == "tag":
        data[2]["meta"] = {}
    elif mutation == "reference":
        data[2]["encounter"]["reference"] = "Encounter/someone-else"
    elif mutation == "quantity":
        data[2]["valueQuantity"]["value"] = 800
    elif mutation == "code":
        data[2]["code"]["coding"][0]["code"] = "wrong"
    else:
        data[0]["birthDate"] = "2020-01-01"
    with pytest.raises(BridgeError):
        await client.read_run(RUN)


async def test_deletion_checks_all_ownership_before_deleting_anything(server_client):
    server, client = server_client
    await client.write_run(RUN)
    list(server.data.values())[-1]["meta"] = {}
    before = copy.deepcopy(server.data)
    with pytest.raises(BridgeError):
        await client.delete_run(RUN)
    assert server.data == before


async def test_write_refuses_to_overwrite_unowned_resource(server_client):
    server, client = server_client
    patient = resources(RUN)[0]
    patient.pop("meta")
    server.data["Patient/" + patient["id"]] = patient
    with pytest.raises(BridgeError):
        await client.write_run(RUN)
    assert server.writes == 0


async def test_malformed_json_and_timeout_are_sanitized():
    for response in [httpx.Response(200, text="secret"), httpx.Response(200, json=[])]:
        async with httpx.AsyncClient(
            base_url="https://fhir.test/",
            transport=httpx.MockTransport(lambda _, response=response: response),
        ) as http:
            with pytest.raises(BridgeError):
                await FhirClient(http).capabilities()

    def timeout(request):
        raise httpx.ReadTimeout("secret", request=request)

    async with httpx.AsyncClient(
        base_url="https://fhir.test/", transport=httpx.MockTransport(timeout)
    ) as http:
        with pytest.raises(BridgeError, match="unavailable"):
            await FhirClient(http).capabilities()


async def test_server_advertising_only_conditional_updates_can_run(server_client):
    server, client = server_client
    for resource in server.capability["rest"][0]["resource"]:
        resource.pop("updateCreate")
        resource["conditionalUpdate"] = True
    original = server.__call__
    requests = []

    def conditional_server(request):
        if request.method == "PUT":
            assert request.url.params["_id"] == json.loads(request.content)["id"]
            requests.append(request)
            # Store under the body's id, as an R4 conditional update must do here.
            body = json.loads(request.content)
            server.data[body["resourceType"] + "/" + body["id"]] = body
            return httpx.Response(201, json=body)
        return original(request)

    async with httpx.AsyncClient(
        base_url="https://fhir.test/fhir/", transport=httpx.MockTransport(conditional_server)
    ) as http:
        assert (await FhirClient(http).write_run(RUN))["verified"] is True
    assert len(requests) == 5
