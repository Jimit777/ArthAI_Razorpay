"""
Encryption at rest for the one secret this platform has to hold.

## What this is for, precisely

Pulling a merchant's settlements needs their Razorpay API secret. Until now the
platform refused to store one at all: it asked at sync time and let it go out of
scope. That is defensible for a person clicking a button and useless for the
thing the product actually needs, which is a nightly pull nobody is awake for.

So the secret gets stored - encrypted, with the key held OUTSIDE the database.
That last clause is the whole point. Encrypting a secret with a key kept beside
it protects against nothing; it just makes the theft two steps instead of one.

## Why Fernet and not something hand-rolled

The standard library has scrypt and HMAC but no AES, and writing a cipher is the
single clearest example of something you must not do yourself. Fernet is
AES-128-CBC with an HMAC-SHA256 authentication tag, from `cryptography`, and it
is designed for exactly this shape of problem: encrypt one short secret, detect
any tampering, no mode or padding decisions left to the caller.

Authentication matters as much as secrecy here. Without the HMAC, someone with
write access to the file could swap the ciphertext for one of their own and the
platform would obediently send a merchant's settlement requests wherever they
pointed it.

## Key rotation

LEDGERLINE_SECRET_KEY may be a comma-separated list. The first key encrypts;
every key is tried when decrypting. That is the standard rotation pattern: add
the new key at the front, let the old one linger until everything has been
re-encrypted, then drop it.

## What this still does not solve

Encryption at rest is one control, not a security posture. Also missing before
this should hold a live merchant's credentials:

  TLS - the session cookie has no Secure flag because there is no HTTPS here
  rate limiting on the login form
  an access log of who read what

Those are named in the UI rather than glossed, and live keys stay refused
unless an operator opts in explicitly.
"""

from __future__ import annotations

import os
from typing import Optional

ENV_KEY = "LEDGERLINE_SECRET_KEY"
ENV_ALLOW_LIVE = "LEDGERLINE_ALLOW_LIVE_KEYS"


class VaultUnavailable(RuntimeError):
    """No key is configured. Refuse to store, never fall back to plaintext."""


class Vault:
    """Encrypts and decrypts short secrets. Nothing else."""

    def __init__(self, keys: list[str]):
        from cryptography.fernet import Fernet, MultiFernet

        if not keys:
            raise VaultUnavailable(f"{ENV_KEY} is not set")
        try:
            self._fernet = MultiFernet([Fernet(k.strip().encode()) for k in keys])
        except Exception as exc:                            # noqa: BLE001
            raise VaultUnavailable(
                f"{ENV_KEY} is not a valid key: {exc}. Generate one with "
                f"`python -m merchant.vault`.") from exc

    # --- construction -----------------------------------------------------

    @classmethod
    def from_env(cls) -> Optional["Vault"]:
        """
        The vault, or None when no key is configured.

        None means "cannot store secrets", never "store them in the clear".
        A silent plaintext fallback is how a file ends up holding credentials
        that everyone believed were encrypted.
        """
        raw = os.environ.get(ENV_KEY, "").strip()
        if not raw:
            return None
        try:
            return cls([k for k in raw.split(",") if k.strip()])
        except VaultUnavailable:
            return None

    @staticmethod
    def generate_key() -> str:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()

    # --- use --------------------------------------------------------------

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            raise ValueError("nothing to encrypt")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> Optional[str]:
        """
        Returns None when the token cannot be authenticated.

        None covers both "wrong key" and "someone edited the ciphertext", and
        the caller should treat them the same: the stored secret is not usable
        and has to be entered again.
        """
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError, TypeError):
            return None

    def rotate(self, token: str) -> Optional[str]:
        """Re-encrypt an existing token under the current primary key."""
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.rotate(token.encode()).decode()
        except (InvalidToken, ValueError, TypeError):
            return None


def live_keys_allowed() -> bool:
    """
    Whether an operator has opted in to live Razorpay credentials.

    Requires BOTH the opt-in and a configured vault. The opt-in alone would let
    someone enable live keys on an install with nowhere safe to put them, which
    is the exact failure this module exists to prevent.
    """
    return (os.environ.get(ENV_ALLOW_LIVE, "").strip() == "1"
            and Vault.from_env() is not None)


def posture() -> dict:
    """What is and is not protected, for the UI to state plainly."""
    vault = Vault.from_env()
    return {
        "encrypted_at_rest": vault is not None,
        "live_keys": live_keys_allowed(),
        "missing": [m for m in (
            None if vault is not None else
            "no encryption key, so secrets cannot be stored at all",
            "no TLS - the session cookie has no Secure flag",
        ) if m],
    }


if __name__ == "__main__":
    print("Generate a key, put it in the environment, and never in this repo:")
    print()
    print(f"  export {ENV_KEY}={Vault.generate_key()}")
    print()
    print("Rotating: put the new key first, keep the old one until everything")
    print("has been re-encrypted, then drop it.")
    print()
    print(f"  export {ENV_KEY}=<new>,<old>")
