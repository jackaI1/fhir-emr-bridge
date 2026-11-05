"""Authenticated field envelopes. Keys are supplied by the operator, never in SQLite."""

import base64
import json
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PrivacyError(Exception):
    """Sanitized error: never include key material, identifiers or plaintext."""


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def decode(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


class EnvelopeCipher:
    def __init__(self, keys: dict[str, bytes], active: str):
        if active not in keys or any(len(k) != 32 for k in keys.values()):
            raise ValueError("An active 256-bit wrapping key is required")
        self.keys, self.active = dict(keys), active

    @staticmethod
    def aad(context: str, purpose: str) -> bytes:
        return canonical(["bridge-field-v1", context, purpose])

    def seal(self, value, context: str) -> dict:
        dek, nonce, wrap_nonce = (
            secrets.token_bytes(32),
            secrets.token_bytes(12),
            secrets.token_bytes(12),
        )
        return {
            "v": 1,
            "kid": self.active,
            "nonce": encode(nonce),
            "wrap_nonce": encode(wrap_nonce),
            "data": encode(AESGCM(dek).encrypt(nonce, canonical(value), self.aad(context, "data"))),
            "wrapped_key": encode(
                AESGCM(self.keys[self.active]).encrypt(
                    wrap_nonce, dek, self.aad(context, "wrap:" + self.active)
                )
            ),
        }

    def unwrap(self, envelope: dict, context: str) -> bytes:
        if envelope["v"] != 1:
            raise ValueError("Unknown envelope version")
        kid = envelope["kid"]
        return AESGCM(self.keys[kid]).decrypt(
            decode(envelope["wrap_nonce"]),
            decode(envelope["wrapped_key"]),
            self.aad(context, "wrap:" + kid),
        )

    def open(self, envelope: dict, context: str):
        try:
            dek = self.unwrap(envelope, context)
            value = AESGCM(dek).decrypt(
                decode(envelope["nonce"]), decode(envelope["data"]), self.aad(context, "data")
            )
            return json.loads(value)
        except (InvalidTag, ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise PrivacyError("Encrypted field could not be authenticated") from exc

    def rewrap(self, envelope: dict, context: str) -> dict:
        try:
            dek = self.unwrap(envelope, context)
            # Authenticate the data as well before declaring rotation successful.
            self.open(envelope, context)
            nonce = secrets.token_bytes(12)
            return {
                **envelope,
                "kid": self.active,
                "wrap_nonce": encode(nonce),
                "wrapped_key": encode(
                    AESGCM(self.keys[self.active]).encrypt(
                        nonce, dek, self.aad(context, "wrap:" + self.active)
                    )
                ),
            }
        except (InvalidTag, ValueError, KeyError, TypeError) as exc:
            raise PrivacyError("Key rotation could not authenticate a field") from exc
