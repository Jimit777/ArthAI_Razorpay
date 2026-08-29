"""
Tests for the TDS credit tracker as a deployed agent rather than as an
engine.

The engine tests (test_tds_rules.py, test_tds_detector.py) prove it reaches
the right conclusion on a generated batch. These prove the platform actually
wires Demo Mode into that engine, persists the result, and shows it back -
which is a different claim, and the one that was missing when the agent was
measurable but not runnable from a browser.
"""

import sys
import time as _time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.tds.taxonomy import TdsCode  # noqa: E402

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


def _run_demo(client, use_agent: str = "no"):
    """Trigger Demo Mode over HTTP, wait for it to finish, return the key."""
    location = client.post(
        "/agents/tds-credit/demo", data={"use_agent": use_agent},
        follow_redirects=False).headers["location"]
    key = location.split("key=")[-1]
    for _ in range(80):
        r = client.get(f"/agents/tds-credit/{key}.json")
        if r.json().get("state") != "running":
            break
        _time.sleep(0.1)
    return key


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


# --- the demo route resolves to a result page -------------------------------

def test_the_demo_route_seeds_and_reconciles_in_one_click(shop):
    key = _run_demo(shop)
    page = shop.get(f"/tds/{key}", follow_redirects=True)
    assert page.status_code == 200
    assert "TDS reconciliation" in page.text or "reconciled" in page.text.lower()


def test_the_agent_is_live_on_the_hub_after_a_run(shop):
    _run_demo(shop)
    page = shop.get("/agents").text
    assert "TDS Credit Tracker" in page
    assert "Coming soon" not in page.split("TDS Credit Tracker")[1][:200]


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


# --- the catalogue flip -----------------------------------------------------

def test_tds_credit_is_registered_live_with_a_runner():
    import merchant.agents.tds_credit  # noqa: F401
    from merchant.catalog import get, live_agents

    spec = get("tds_credit")
    assert spec is not None
    assert spec.is_live
    assert spec.runner is not None
    assert "tds_credit" in {a.id for a in live_agents()}


# --- findings carry what the results page needs -----------------------------

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
