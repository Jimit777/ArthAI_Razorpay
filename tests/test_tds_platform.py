"""
Tests for the TDS credit tracker's Ledger layer.

`tds_credit` is not currently a live agent (see merchant/catalog.py's
why_unbuilt: neither side of this reconciliation has anything resembling
an API, and the only real-data path would need a merchant to manually
cross-reference both documents before the tool ever ran). These tests exist
because the Ledger persistence underneath it - seeding, batch assembly,
committing a run, recording findings - is real, tested, working code kept
in the repo as groundwork, not something torn out along with the routes.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

PASSWORD = "a-good-password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


@pytest.fixture
def shop(client):
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    return client


def _seed(client, n: int = 24):
    import merchant.app as appmod

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        return led.seed_tds_demo(n)


# --- demo seeding writes both tables ----------------------------------------

def test_seeding_the_demo_writes_both_tables(shop):
    n = _seed(shop)
    import merchant.app as appmod

    with appmod.ledger() as led:
        deductions = led.conn.execute(
            "SELECT COUNT(*) c FROM live_tds_deductions").fetchone()["c"]
        credits = led.conn.execute(
            "SELECT COUNT(*) c FROM live_tds_credits").fetchone()["c"]
    assert deductions == n
    assert credits > 0
    assert credits < deductions, "some deductions must have no credit yet"


# --- commit marks the deductions reconciled ---------------------------------

def test_committing_a_run_marks_its_deductions_reconciled(shop):
    _seed(shop, 20)
    import merchant.app as appmod
    from engine.tds.detector import detect_batch
    from engine.tds.gate import gate_batch

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        batch = led.build_tds_batch()
        variances = detect_batch(batch)
        decisions = gate_batch(variances, [])
        run_id = led.commit_tds_run(batch)
        led.record_tds_findings(run_id, variances, decisions)

        remaining = led.build_tds_batch(only_unreconciled=True)
        assert remaining is None, \
            "a committed run must not leave its own rows unreconciled"

        findings = led.tds_findings(run_id)
        assert len(findings) == 20


def test_a_second_seed_does_not_touch_already_reconciled_rows(shop):
    _seed(shop, 10)
    import merchant.app as appmod

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        first_batch = led.build_tds_batch()
        run_id = led.commit_tds_run(first_batch)
        assert run_id

        led.seed_tds_demo(5, seed=99)
        remaining = led.build_tds_batch()
        assert remaining is not None
        assert len(remaining.deductions) == 5, \
            "only the newly seeded rows should be unreconciled"


# --- findings carry what a results page would need --------------------------

def test_findings_carry_the_regime_split(shop):
    _seed(shop, 40)
    import merchant.app as appmod
    from engine.tds.detector import detect_batch
    from engine.tds.gate import gate_batch

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        batch = led.build_tds_batch()
        variances = detect_batch(batch)
        decisions = gate_batch(variances, [])
        run_id = led.commit_tds_run(batch)
        led.record_tds_findings(run_id, variances, decisions)
        findings = led.tds_findings(run_id)

    pre = sum(1 for f in findings if f["deducted_at"] < "2026-04-01")
    post = len(findings) - pre
    assert pre > 0
    assert post > 0
