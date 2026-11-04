"""Exercise actual OpenEMR SMART login/consent and FHIR reads, using synthetic data.

Only the disposable compose demo is targeted. No listener/browser is created.
The real authorization-code/PKCE flow is driven over HTTP, not a password grant.
"""

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from fhir_emr_bridge.app import create_app
from fhir_emr_bridge.config import Settings

BASE = "https://localhost:9300"
CALLBACK = "http://127.0.0.1:8000/auth/callback"
SCOPES = (
    "openid offline_access api:fhir user/Patient.read user/Encounter.read "
    "user/Observation.read api:oemr user/patient.read user/patient.write "
    "user/encounter.read user/encounter.write user/vital.read user/vital.write"
)


def compose(*args, client_id=None):
    command = [
        os.getenv("COMPOSE_BIN", "docker"),
        *([] if os.getenv("COMPOSE_BIN") else ["compose"]),
        "-f",
        "compose.openemr.yaml",
        *args,
    ]
    environment = dict(os.environ)
    if client_id:
        environment["BRIDGE_DEMO_CLIENT_ID"] = client_id
    result = subprocess.run(command, check=True, env=environment, capture_output=True, text=True)
    if "Disposable demo APIs configured." not in result.stdout:
        raise RuntimeError(
            "OpenEMR CLI bootstrap did not reach configuration; check container setup"
        )


def local_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "localhost"
        or parsed.port != 9300
        or parsed.username
        or parsed.fragment
    ):
        raise RuntimeError("Demo login attempted to leave the isolated localhost server")
    return value


def login_and_consent(browser, authorization_url, password):
    response = browser.get(local_url(authorization_url))
    for _ in range(15):
        if response.is_redirect:
            target = urljoin(str(response.url), response.headers["location"])
            if target.startswith(CALLBACK + "?"):
                return parse_qs(urlsplit(target).query)
            response = browser.get(local_url(target))
            continue
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", id="userLogin")
        if form is None:
            raise RuntimeError("Expected the OpenEMR OAuth login/consent form")
        data = {}
        for field in form.select("input[name]"):
            if field.get("type") in {"checkbox", "radio"} and not field.has_attr("checked"):
                continue
            data[field["name"]] = field.get("value", "")
        if form.select_one('input[name="password"]'):
            data.update(username="admin", password=password, user_role="api")
        else:
            # Equivalent to selecting the app's registered scopes in this disposable demo.
            # Consent's JS normally reconstructs these hidden scope inputs.
            data.update({f"scope[{scope}]": scope for scope in SCOPES.split()})
            data["proceed"] = "1"
        target = local_url(urljoin(str(response.url), form.get("action", "")))
        response = browser.post(target, data=data)
    raise RuntimeError("OpenEMR login/consent did not complete within 15 steps")


def register():
    with httpx.Client(verify=False, timeout=60, follow_redirects=False) as browser:
        registration = browser.post(
            BASE + "/oauth2/default/registration",
            json={
                "application_type": "private",
                "client_name": "Synthetic FHIR Bridge",
                "redirect_uris": [CALLBACK],
                "scope": SCOPES,
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
        if not registration.is_success:
            raise RuntimeError(f"OpenEMR registration failed (HTTP {registration.status_code})")
        client_id = registration.json()["client_id"]
        client_secret = registration.json()["client_secret"]
    compose(
        "exec",
        "-T",
        "-e",
        f"BRIDGE_DEMO_CLIENT_ID={client_id}",
        "openemr",
        "php",
        "/tmp/configure_openemr.php",
    )
    secret_path = Path(".local/openemr-client-secret")
    descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(client_secret)
    # Keep the client ID locally alongside its separately protected secret.
    with open(".local/openemr-client-id", "w") as output:
        output.write(client_id)
    return client_id, client_secret


def main():
    os.makedirs(".local", exist_ok=True)
    password = dotenv_values(".env").get("DEMO_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("Run scripts/demo_env.py and start compose.openemr.yaml first")
    compose("exec", "-T", "openemr", "php", "/tmp/configure_openemr.php")
    client_id, client_secret = register()
    app = create_app(
        Settings(
            fhir_base_url=BASE + "/apis/default/fhir",
            smart_client_id=client_id,
            smart_client_secret=client_secret,
            smart_scopes=SCOPES,
            allow_insecure_local_tls=True,
            openemr_seed_enabled=True,
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8000", follow_redirects=False) as client:
        metadata = client.get("/capabilities")
        assert metadata.status_code == 200, metadata.text
        login = client.get("/auth/login")
        assert login.status_code == 302, login.text
        with httpx.Client(verify=False, timeout=60, follow_redirects=False) as browser:
            params = login_and_consent(browser, login.headers["location"], password)
        if "error" in params:
            raise RuntimeError("OpenEMR denied the registered demo authorization")
        callback = client.get("/auth/callback", params={k: v[0] for k, v in params.items()})
        assert callback.status_code == 200, callback.text
        seeded = client.post("/openemr/demo")
        assert seeded.status_code == 200, seeded.text
        result = seeded.json()
        assert result["encounter_count"] >= 1, result
        assert result["observation_count"] >= 3, result
        assert {"8867-4", "8310-5", "29463-7"} <= {obs["code"] for obs in result["observations"]}, (
            result
        )
        read = client.get("/openemr/patients/" + result["patient_id"])
        assert read.status_code == 200, read.text
        assert read.json()["observation_count"] == result["observation_count"]
        # This is expected to fail BEFORE writing: OpenEMR does not advertise these FHIR writes.
        from uuid import uuid4

        unsupported = client.post("/demo/" + str(uuid4()))
        assert unsupported.status_code == 409, unsupported.text
        assert client.post("/auth/logout").status_code == 200
        print(
            json.dumps(
                {
                    "smart_authorized": True,
                    "pkce": "S256",
                    "fhir_version": metadata.json()["fhirVersion"],
                    **result,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
