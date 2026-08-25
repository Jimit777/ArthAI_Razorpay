"""
Tests for encryption at rest.

Encryption is the kind of thing that looks like it works whether or not it
does: a ciphertext is unreadable either way, and a bug shows up only when
someone with the file reads a credential out of it. So these tests mostly
inspect the bytes and try to break the guarantees.

The guarantee itself is narrow and worth stating: the secret is encrypted with
a key held OUTSIDE the database. Encrypting it with a key kept beside it
protects against nothing - it makes the theft two steps instead of one.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.sources import SourceKind, Sources  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402
from merchant.vault import (  # noqa: E402
    ENV_ALLOW_LIVE,
    ENV_KEY,
    Vault,
    VaultUnavailable,
    live_keys_allowed,
    posture,
)

SECRET = "KxrdFjpw24L1cA9HrVWgSu4b"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(ENV_KEY, Vault.generate_key())
    return Vault.from_env()


# --- the cipher ----------------------------------------------------------

def test_a_round_trip_returns_the_secret(keyed):
    assert keyed.decrypt(keyed.encrypt(SECRET)) == SECRET


def test_the_ciphertext_does_not_contain_the_secret(keyed):
    assert SECRET not in keyed.encrypt(SECRET)


def test_the_same_secret_encrypts_differently_every_time(keyed):
    """A deterministic ciphertext tells an observer when two values match."""
    assert keyed.encrypt(SECRET) != keyed.encrypt(SECRET)


def test_another_key_cannot_read_it(monkeypatch):
    monkeypatch.setenv(ENV_KEY, Vault.generate_key())
    token = Vault.from_env().encrypt(SECRET)

    monkeypatch.setenv(ENV_KEY, Vault.generate_key())
    assert Vault.from_env().decrypt(token) is None


def test_a_tampered_ciphertext_is_rejected(keyed):
    """
    Authentication matters as much as secrecy. Without it, someone with write
    access could swap the ciphertext for one of their own and the platform
    would send a merchant's settlement requests wherever they pointed it.
    """
    token = keyed.encrypt(SECRET)
    edited = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert keyed.decrypt(edited) is None
    assert keyed.decrypt("not a token at all") is None
    assert keyed.decrypt("") is None


# --- configuration -------------------------------------------------------

def test_no_key_means_no_vault_not_a_plaintext_fallback(monkeypatch):
    """
    None means "cannot store secrets", never "store them in the clear". A
    silent fallback is how a file ends up holding credentials everyone believed
    were encrypted.
    """
    monkeypatch.delenv(ENV_KEY, raising=False)
    assert Vault.from_env() is None


def test_a_malformed_key_is_not_silently_accepted(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "obviously-not-a-fernet-key")
    assert Vault.from_env() is None
    with pytest.raises(VaultUnavailable):
        Vault(["obviously-not-a-fernet-key"])


def test_keys_can_be_rotated(monkeypatch):
    """
    New key first, old key kept until everything is re-encrypted, then dropped.
    Anything encrypted under either must stay readable in the meantime.
    """
    old_key, new_key = Vault.generate_key(), Vault.generate_key()

    monkeypatch.setenv(ENV_KEY, old_key)
    token = Vault.from_env().encrypt(SECRET)

    monkeypatch.setenv(ENV_KEY, f"{new_key},{old_key}")
    both = Vault.from_env()
    assert both.decrypt(token) == SECRET

    rotated = both.rotate(token)
    monkeypatch.setenv(ENV_KEY, new_key)
    assert Vault.from_env().decrypt(rotated) == SECRET
    assert Vault.from_env().decrypt(token) is None, "the old key is gone"


# --- live keys -----------------------------------------------------------

def test_live_keys_need_both_a_vault_and_an_opt_in(monkeypatch):
    """
    The opt-in alone would enable live credentials on an install with nowhere
    safe to put them - the exact failure the vault exists to prevent.
    """
    monkeypatch.delenv(ENV_KEY, raising=False)
    monkeypatch.setenv(ENV_ALLOW_LIVE, "1")
    assert not live_keys_allowed()

    monkeypatch.setenv(ENV_KEY, Vault.generate_key())
    assert live_keys_allowed()

    monkeypatch.delenv(ENV_ALLOW_LIVE, raising=False)
    assert not live_keys_allowed()


def test_a_live_key_is_refused_by_default(monkeypatch):
    from merchant.sources import Razorpay

    monkeypatch.delenv(ENV_ALLOW_LIVE, raising=False)
    with pytest.raises(ValueError, match="test-mode keys only"):
        Razorpay("rzp_live_something", "secret")


def test_the_posture_names_what_is_still_missing(monkeypatch):
    """
    An install that looks safe and is not is worse than one that admits what it
    lacks. Encryption at rest is one control, not a security posture.
    """
    monkeypatch.setenv(ENV_KEY, Vault.generate_key())
    state = posture()
    assert state["encrypted_at_rest"]
    missing = " ".join(state["missing"])
    assert "TLS" in missing
    # Rate limiting and the access log were both on this list until they were
    # built. A posture that keeps claiming a gap after it is closed is as
    # misleading as one that hides it, so the list shrinks as things land.
    assert "rate limiting" not in missing
    assert "access log" not in missing


# --- stored on a real connection ----------------------------------------

def test_a_stored_secret_survives_a_restart(tmp_path, monkeypatch):
    """The point of storing it: a nightly sync nobody is awake for."""
    monkeypatch.setenv(ENV_KEY, Vault.generate_key())

    led = Ledger(tmp_path / "v.db")
    biz = led.businesses.create("Sync Co")
    sources = Sources(led.conn)
    sources._set(biz, SourceKind.RAZORPAY, "rzp_test_id", "ok", "Connected.",
                 Vault.from_env().encrypt(SECRET))
    led.close()

    again = Ledger(tmp_path / "v.db")
    assert Sources(again.conn).stored_secret(biz) == SECRET
    again.close()


def test_without_the_key_the_stored_secret_is_unreadable(tmp_path, monkeypatch):
    """A copy of the database is not a copy of the credential."""
    monkeypatch.setenv(ENV_KEY, Vault.generate_key())
    led = Ledger(tmp_path / "v.db")
    biz = led.businesses.create("Sync Co")
    sources = Sources(led.conn)
    sources._set(biz, SourceKind.RAZORPAY, "rzp_test_id", "ok", "Connected.",
                 Vault.from_env().encrypt(SECRET))
    led.close()

    monkeypatch.delenv(ENV_KEY, raising=False)
    again = Ledger(tmp_path / "v.db")
    assert Sources(again.conn).stored_secret(biz) is None
    again.close()


def test_switching_to_the_simulator_drops_the_stored_secret(tmp_path, monkeypatch):
    """
    Keeping a credential for a connection nobody is using is how one outlives
    the reason it existed.
    """
    monkeypatch.setenv(ENV_KEY, Vault.generate_key())
    led = Ledger(tmp_path / "v.db")
    biz = led.businesses.create("Switch Co")
    sources = Sources(led.conn)
    sources._set(biz, SourceKind.RAZORPAY, "rzp_test_id", "ok", "Connected.",
                 Vault.from_env().encrypt(SECRET))
    assert sources.stored_secret(biz) == SECRET

    sources.use_simulator(biz)
    assert sources.stored_secret(biz) is None
    led.close()


def test_without_a_vault_nothing_is_stored_at_all(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    led = Ledger(tmp_path / "v.db")
    biz = led.businesses.create("No Vault Co")
    sources = Sources(led.conn)
    sources._set(biz, SourceKind.RAZORPAY, "rzp_test_id", "ok", "Connected.")

    assert sources.get(biz)["razorpay_secret_encrypted"] is None
    assert sources.stored_secret(biz) is None
    led.close()
