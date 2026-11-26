"""Field-level encryption and keyed hashing.

* AES-256-GCM for sensitive fields. Each ciphertext is bound (via associated
  data) to the document and field it belongs to, so a ciphertext copied from
  one document into another fails to decrypt. That blocks e.g. copying a large
  balance from one user to another directly in the database.
* Keys carry a version prefix so they can be rotated: the first key in
  ENCRYPTION_KEYS encrypts, every listed key can decrypt.
* HMAC-SHA256 turns Telegram user IDs into opaque document IDs, so the database
  alone does not reveal who owns an account.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


class DecryptionError(Exception):
    pass


def _b64decode(value: str) -> bytes:
    value = value.strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_key() -> str:
    return _b64encode(os.urandom(32))


class Crypto:
    def __init__(self, encryption_keys: str, hmac_key: str):
        """encryption_keys: "v2:<b64>,v1:<b64>" — first entry is the active key."""
        self._keys: dict[str, AESGCM] = {}
        self._active: str | None = None
        for entry in encryption_keys.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
                raise ValueError("Each ENCRYPTION_KEYS entry must look like 'v1:<base64 key>'")
            version, b64 = entry.split(":", 1)
            if not version.isalnum():
                raise ValueError(f"Invalid key version {version!r}")
            raw = _b64decode(b64)
            if len(raw) != 32:
                raise ValueError(f"Key {version} must be 32 bytes (got {len(raw)})")
            if version in self._keys:
                raise ValueError(f"Duplicate key version {version}")
            self._keys[version] = AESGCM(raw)
            if self._active is None:
                self._active = version
        if self._active is None:
            raise ValueError("ENCRYPTION_KEYS is empty")

        hk = _b64decode(hmac_key)
        if len(hk) < 32:
            raise ValueError("HMAC_KEY must be at least 32 bytes")
        self._hmac_key = hk

    # -- encryption -------------------------------------------------------
    def encrypt(self, plaintext: str, context: str) -> str:
        nonce = os.urandom(_NONCE_LEN)
        ct = self._keys[self._active].encrypt(nonce, plaintext.encode(), context.encode())
        return f"{self._active}:{_b64encode(nonce + ct)}"

    def decrypt(self, token: str, context: str) -> str:
        try:
            version, payload = token.split(":", 1)
            key = self._keys[version]
            raw = _b64decode(payload)
            return key.decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], context.encode()).decode()
        except (ValueError, KeyError, InvalidTag) as exc:
            raise DecryptionError(f"Cannot decrypt field ({context})") from exc

    def needs_rotation(self, token: str) -> bool:
        return not token.startswith(f"{self._active}:")

    # -- hashing ----------------------------------------------------------
    def user_hash(self, telegram_id: int) -> str:
        return hmac.new(self._hmac_key, f"tg:{telegram_id}".encode(), hashlib.sha256).hexdigest()
