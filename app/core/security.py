"""Encryption helpers for storing third-party credentials at rest (ported from organize-me).

Storage-provider credentials (Google Drive/Dropbox OAuth tokens, S3 keys - see the
`storage_configs` table) must never be persisted in plaintext. `CredentialCipher` wraps Fernet
(authenticated symmetric encryption from `cryptography`) to encrypt on write and decrypt on read.

The cipher takes its key explicitly rather than reaching into global settings, so it's trivially
unit-testable with a throwaway generated key. Application code obtains the configured instance
via `get_credential_cipher()`, which reads `ENCRYPTION_KEY` from settings.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class CredentialCipher:
    """Encrypts/decrypts short credential strings with a Fernet key.

    `key` must be a urlsafe-base64-encoded 32-byte key, i.e. the output of
    `cryptography.fernet.Fernet.generate_key()`.
    """

    def __init__(self, key: str | bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """Return an encrypted, urlsafe-base64 token for `plaintext`.

        Fernet output is non-deterministic (each call embeds a fresh timestamp + IV), so the
        same input yields a different token each time - that's expected, not a bug.
        """
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Return the original plaintext for a token produced by `encrypt`.

        Raises `cryptography.fernet.InvalidToken` if the token was tampered with or was
        produced with a different key.
        """
        return self._fernet.decrypt(token.encode()).decode()


@lru_cache
def get_credential_cipher() -> CredentialCipher:
    """The application-wide cipher, keyed from the ENCRYPTION_KEY setting.

    Raises RuntimeError (not a cryptic Fernet error) if ENCRYPTION_KEY isn't configured, so a
    misconfigured deployment fails with an actionable message the first time a credential
    encrypt/decrypt is attempted.
    """
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set - required to encrypt/decrypt stored storage credentials. "
            "Generate one with `python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"` and set it in the environment.'
        )
    return CredentialCipher(key)


__all__ = ["CredentialCipher", "InvalidToken", "get_credential_cipher"]
