"""Криптографическая личность узла (ТЗ 4): Ed25519, device_id = SHA-256(public key), TOFU."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def fingerprint(public_key_raw: bytes) -> str:
    """device_id / отпечаток: hex SHA-256 от 32 байт публичного ключа."""
    return hashlib.sha256(public_key_raw).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


class Identity:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._priv = private_key
        self.public_key_raw: bytes = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.device_id: str = fingerprint(self.public_key_raw)

    @property
    def public_key_b64(self) -> str:
        return b64(self.public_key_raw)

    @property
    def short_id(self) -> str:
        return self.device_id[:8]

    def sign(self, data: bytes) -> bytes:
        return self._priv.sign(data)

    @staticmethod
    def verify(public_key_raw: bytes, signature: bytes, data: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(public_key_raw).verify(signature, data)
            return True
        except (InvalidSignature, ValueError):
            return False

    @classmethod
    def load_or_create(cls, key_file: Path) -> "Identity":
        """Первый запуск — генерируем пару ключей. Приватный ключ никогда не покидает ПК."""
        if key_file.exists():
            raw = key_file.read_bytes()
            priv = serialization.load_pem_private_key(raw, password=None)
            if not isinstance(priv, Ed25519PrivateKey):
                raise ValueError("device.key: неверный тип ключа")
            return cls(priv)
        priv = Ed25519PrivateKey.generate()
        pem = priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        key_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = key_file.with_suffix(".tmp")
        tmp.write_bytes(pem)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, key_file)
        return cls(priv)
