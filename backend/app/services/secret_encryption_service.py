"""Authenticated encryption for short-lived provider credentials."""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..environment import get_xmax_temp_key_encryption_key


class SecretEncryptionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _fernet() -> Fernet:
    key = get_xmax_temp_key_encryption_key()
    if not key:
        raise SecretEncryptionError(
            "XMAX_AUTH_NOT_CONFIGURED",
            "Temporary-key encryption is not configured.",
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise SecretEncryptionError(
            "XMAX_AUTH_NOT_CONFIGURED",
            "Temporary-key encryption configuration is invalid.",
        ) from error


def encrypt_temporary_key(temporary_key: str) -> str:
    if not temporary_key.startswith("tk-"):
        raise SecretEncryptionError("XMAX_RESPONSE_INVALID", "Xmax returned an invalid temporary key.")
    return _fernet().encrypt(temporary_key.encode("utf-8")).decode("ascii")


def decrypt_temporary_key(ciphertext: str) -> str:
    try:
        value = _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as error:
        raise SecretEncryptionError(
            "XMAX_INTERNAL_ERROR",
            "The stored temporary key cannot be decrypted.",
        ) from error
    if not value.startswith("tk-"):
        raise SecretEncryptionError("XMAX_INTERNAL_ERROR", "The stored temporary key is invalid.")
    return value


def temporary_key_fingerprint(temporary_key: str) -> str:
    return hashlib.sha256(temporary_key.encode("utf-8")).hexdigest()


def temporary_key_last4(temporary_key: str) -> str:
    return temporary_key[-4:]
