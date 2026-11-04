"""Operator-owned configuration; request input never chooses an upstream."""

from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("URL must have a host and no credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK:
        raise ValueError("HTTP is allowed only on loopback")
    return value.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BRIDGE_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    fhir_base_url: str = "https://hapi.fhir.org/baseR4"
    smart_client_id: str = ""
    smart_client_secret: SecretStr = SecretStr("")
    smart_scopes: str = (
        "openid offline_access api:fhir user/Patient.read user/Encounter.read user/Observation.read"
    )
    redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    allow_insecure_local_tls: bool = False
    openemr_seed_enabled: bool = False
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]", "testserver"]

    @model_validator(mode="after")
    def validate_urls(self):
        self.fhir_base_url = safe_url(self.fhir_base_url)
        self.redirect_uri = safe_url(self.redirect_uri)
        local = urlsplit(self.fhir_base_url).hostname in LOOPBACK
        if self.allow_insecure_local_tls and not local:
            raise ValueError("TLS verification may be disabled only for loopback demo servers")
        if self.openemr_seed_enabled and (not local or not self.smart_client_id):
            raise ValueError("OpenEMR seeding requires a loopback server and SMART client")
        return self
