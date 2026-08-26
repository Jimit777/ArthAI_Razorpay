"""
Tests for the forward cash forecaster.

Two things run through all of it.

Every number is computed before the model is asked. A forecast is a chain of
thirty additions where each day depends on the last, and a model that is right
ninety-nine times out of a hundred per step is wrong about the month. So the
schema has nowhere to put a rupee figure and the action comes off a ladder.

And the agent never suggests moving something that cannot move. Payroll and
statutory dues are unmovable as a property of the record, not as an opinion,
because advising a merchant to delay a salary is advising a default.
"""

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.treasury_classifier import (TreasuryJudgment, movable_ids,  # noqa: E402
                                       review, strict_schema,
                                       unverified_figures)
from agent.treasury_prompt import render  # noqa: E402
from engine.treasury.forecaster import (SAFE_FLOOR_PAISE,  # noqa: E402
                                        project_cash_flow)
from engine.treasury.records import (ACT_DELAY_PAYOUT,  # noqa: E402
                                     ACT_DRAW_CREDIT_LINE, ACT_NONE,
                                     CASH_CRUNCH_WARNING, CASH_HEALTHY,
                                     CASH_OVERDRAWN, KIND_PAYROLL,
                                     KIND_STATUTORY, KIND_VENDOR, BankAccount,
                                     ExpectedReceipt, RecurringExpense,
                                     ScheduledPayout, TreasuryInputs)
from generator.synthetic_treasury import CRUNCH_DAY, generate  # noqa: E402
from merchant.treasury_pipeline import run, score  # noqa: E402

PASSWORD = "a-good-password"
TODAY = date(2026, 8, 27)


def _inputs(balance=10_00_000_00, payouts=(), receipts=(), recurring=()):
    return TreasuryInputs(
        accounts=[BankAccount("acc", "Test", balance, TODAY)],
        payouts=list(payouts), receipts=list(receipts),
        recurring=list(recurring), as_of=TODAY)


# --- the arithmetic -------------------------------------------------------

def test_the_balance_carries_forward_day_by_day():
    """The recurrence, asserted directly. Everything else rests on it."""
    forecast = project_cash_flow(_inputs(
        balance=1_00_000_00,
        receipts=[ExpectedReceipt("r1", "gateway", 20_000_00,
                                  TODAY + timedelta(days=3))],
        payouts=[ScheduledPayout("p1", "Vendor", 30_000_00,
                                 TODAY + timedelta(days=5))]))

    assert forecast.positions[0].closing == 1_00_000_00
    assert forecast.positions[2].closing == 1_20_000_00       # day 3, receipt
    assert forecast.positions[4].closing == 90_000_00         # day 5, payout
    assert forecast.positions[-1].closing == 90_000_00


def test_every_day_is_an_integer_number_of_paise():
    """Thirty days of floats accumulates error into the figure somebody acts
    on."""
    inputs, _ = generate(as_of=TODAY)
    for position in project_cash_flow(inputs).positions:
        assert isinstance(position.closing, int)
        assert isinstance(position.receipts, int)


def test_the_trough_is_the_lowest_closing_balance():
    inputs, _ = generate(as_of=TODAY)
    forecast = project_cash_flow(inputs)
    assert forecast.trough.balance == min(p.closing
                                          for p in forecast.positions)


def test_a_month_end_charge_lands_in_a_short_month():
    """
    A charge set for the 31st still has to come out in a 30-day month, and the
    last day is where banks take it. Dropping it would understate the outflow
    in exactly the months that are tightest.
    """
    forecast = project_cash_flow(TreasuryInputs(
        accounts=[BankAccount("acc", "Test", 5_00_000_00, date(2026, 9, 1))],
        recurring=[RecurringExpense("Rent", 1_00_000_00, day_of_month=31)],
        as_of=date(2026, 9, 1)))

    charged = [p for p in forecast.positions if p.recurring]
    assert charged, "the charge vanished in a thirty-day month"
    assert charged[0].on.day == 30


# --- the findings ---------------------------------------------------------

def test_a_comfortable_month_is_not_flagged():
    forecast = project_cash_flow(_inputs(balance=50_00_000_00))
    assert forecast.finding == CASH_HEALTHY
    assert forecast.action == ACT_NONE


def test_dropping_below_the_floor_is_a_crunch():
    forecast = project_cash_flow(_inputs(
        balance=1_00_000_00,
        payouts=[ScheduledPayout("p1", "Vendor", 80_000_00,
                                 TODAY + timedelta(days=4))]))
    assert forecast.finding == CASH_CRUNCH_WARNING
    assert forecast.trough.shortfall == SAFE_FLOOR_PAISE - 20_000_00


def test_going_negative_is_its_own_finding():
    forecast = project_cash_flow(_inputs(
        balance=50_000_00,
        payouts=[ScheduledPayout("p1", "Vendor", 2_00_000_00,
                                 TODAY + timedelta(days=4))]))
    assert forecast.finding == CASH_OVERDRAWN
    assert forecast.trough.below_zero is True


# --- scheduling problem or funding problem --------------------------------

def test_a_movable_payout_makes_it_a_scheduling_problem():
    forecast = project_cash_flow(_inputs(
        balance=1_00_000_00,
        payouts=[ScheduledPayout("V-1", "Vendor", 80_000_00,
                                 TODAY + timedelta(days=4), KIND_VENDOR)]))
    assert forecast.coverable_by_delay is True
    assert forecast.action == ACT_DELAY_PAYOUT


def test_an_unmovable_cluster_is_a_funding_problem():
    """
    The distinction that matters most. Telling a merchant to shuffle payments
    when they need a credit line loses them the week they needed to arrange
    it.
    """
    forecast = project_cash_flow(_inputs(
        balance=1_00_000_00,
        payouts=[
            ScheduledPayout("PAYROLL", "Payroll", 90_000_00,
                            TODAY + timedelta(days=4), KIND_PAYROLL),
            ScheduledPayout("TDS", "Advance tax", 40_000_00,
                            TODAY + timedelta(days=4), KIND_STATUTORY)]))

    assert forecast.coverable_by_delay is False
    assert forecast.action == ACT_DRAW_CREDIT_LINE
    assert forecast.movable_total == 0
    assert len(forecast.unmovable_near_trough) == 2


def test_payroll_and_statutory_dues_are_never_movable():
    for kind in (KIND_PAYROLL, KIND_STATUTORY):
        payout = ScheduledPayout("x", "y", 1000, TODAY, kind)
        assert payout.movable is False
        assert payout.delay_days == 0


# --- the planted scenario -------------------------------------------------

def test_the_demo_plants_a_crunch_and_the_engine_finds_it():
    """
    THE test for the demo. The scenario is built to break on a particular day
    in a particular way; if a tuning change stops the plant taking, this fails
    rather than the demo quietly becoming a flat line. That has happened once
    already.
    """
    inputs, planted = generate(as_of=TODAY)
    forecast = project_cash_flow(inputs)
    result = score(forecast, planted)

    assert result["all_passed"], result["checks"]
    assert forecast.trough.day == CRUNCH_DAY
    assert forecast.finding == CASH_CRUNCH_WARNING
    assert forecast.trough.shortfall > 0


def test_the_relief_lands_after_the_trough_not_before():
    """What makes the demo a scheduling problem rather than a funding one."""
    inputs, planted = generate(as_of=TODAY)
    forecast = project_cash_flow(inputs)

    assert forecast.coverable_by_delay is True
    assert forecast.receipts_after_trough > forecast.trough.shortfall
    relief = [p for p in forecast.positions
              if p.day == planted["relief_lands_on_day"]]
    assert relief and relief[0].receipts >= 3_00_000_00


def test_the_scenario_is_the_same_every_time():
    first, _ = generate(as_of=TODAY)
    second, _ = generate(as_of=TODAY)
    assert [p.amount for p in first.payouts] == [p.amount for p in second.payouts]
    assert (project_cash_flow(first).trough.balance
            == project_cash_flow(second).trough.balance)


# --- the agent may judge, never compute -----------------------------------

def test_the_schema_has_nowhere_to_put_a_rupee():
    fields = strict_schema()["properties"]
    assert set(fields) == {"exception_code", "action", "hold_payout_id",
                           "hold_days", "confidence", "reasoning"}
    # hold_days is the only integer, and it is a count of days, not money.
    assert "amount" not in fields and "balance" not in fields
    assert "shortfall" not in fields


def test_an_invented_figure_discards_the_advice():
    inputs, _ = generate(as_of=TODAY)
    forecast = project_cash_flow(inputs)
    judged = TreasuryJudgment(
        exception_code=forecast.finding, action=ACT_DELAY_PAYOUT,
        hold_payout_id="V-1042", hold_days=3, confidence=0.9,
        reasoning="You will be Rs 7,77,777.00 short, so move it.")

    verdict = review(forecast, judged, render(forecast))
    assert verdict.invented_figures
    assert verdict.confidence == 0.0
    assert verdict.reasoning == forecast.detail


def test_a_payout_id_nobody_supplied_is_refused():
    """
    Worse than an invented number, because it reads as specific. A controller
    could act on "delay V-9999" before noticing there is no such invoice.
    """
    inputs, _ = generate(as_of=TODAY)
    forecast = project_cash_flow(inputs)
    judged = TreasuryJudgment(
        exception_code=forecast.finding, action=ACT_DELAY_PAYOUT,
        hold_payout_id="V-9999", hold_days=3, confidence=0.9,
        reasoning="Move it.")

    verdict = review(forecast, judged, render(forecast))
    assert verdict.hold_payout_id is None
    assert verdict.hold_days is None
    assert any("not in the movable list" in c for c in verdict.corrections)


def test_a_supplied_payout_id_is_kept():
    inputs, _ = generate(as_of=TODAY)
    forecast = project_cash_flow(inputs)
    allowed = sorted(movable_ids(forecast))
    assert allowed

    judged = TreasuryJudgment(
        exception_code=forecast.finding, action=ACT_DELAY_PAYOUT,
        hold_payout_id=allowed[0], hold_days=3, confidence=0.85,
        reasoning="Move it.")
    verdict = review(forecast, judged, render(forecast))

    assert verdict.hold_payout_id == allowed[0]
    assert verdict.hold_days == 3


def test_the_agent_may_not_relax_the_action():
    inputs, _ = generate(as_of=TODAY)
    forecast = project_cash_flow(inputs)
    relaxed = TreasuryJudgment(
        exception_code=forecast.finding, action=ACT_NONE, confidence=0.9,
        reasoning="Looks fine.")

    verdict = review(forecast, relaxed, render(forecast))
    assert verdict.action == forecast.action
    assert verdict.agent_action == ACT_NONE
    assert verdict.corrections


def test_the_finding_is_the_engines_not_the_agents():
    """It is a description of the balance, not an opinion about it."""
    inputs, _ = generate(as_of=TODAY)
    forecast = project_cash_flow(inputs)
    wrong = TreasuryJudgment(
        exception_code="CASH_HEALTHY", action=forecast.action,
        confidence=0.9, reasoning="Fine.")

    verdict = review(forecast, wrong, render(forecast))
    assert verdict.exception_code == forecast.finding
    assert any("the agent called it" in c for c in verdict.corrections)


def test_the_evidence_separates_movable_from_unmovable():
    inputs, _ = generate(as_of=TODAY)
    evidence = render(project_cash_flow(inputs))

    assert "CAN BE MOVED" in evidence
    assert "CANNOT BE MOVED" in evidence
    assert "PAY-PAYROLL-08" in evidence
    assert "SHORTFALL BELOW FLOOR" in evidence


def test_whole_rupee_formatting_is_not_an_invention():
    assert unverified_figures("short by Rs 43,311", "Rs 43,311.00 short") == []


# --- the run works without a model at all ---------------------------------

def test_the_forecast_is_actionable_without_the_agent():
    inputs, planted = generate(as_of=TODAY)
    payload = run(inputs, use_agent=False, planted=planted).as_dict()

    assert payload["metadata"]["usage"]["usd"] == 0
    assert payload["verdict"] is None
    forecast = payload["forecast"]
    assert forecast["action"] == ACT_DELAY_PAYOUT
    assert forecast["trough"]["shortfall"] > 0
    assert len(forecast["positions"]) == 30


# --- inferring what recurs ------------------------------------------------

def test_recurring_charges_are_inferred_when_none_are_uploaded():
    """
    A forecast that silently omits rent is cheerful and wrong in the direction
    that hurts. Absent a file, history is read instead.
    """
    from merchant.treasury_import import infer_recurring

    history = []
    for month in (5, 6, 7):
        history.append(ScheduledPayout(
            f"R-{month}", "Office Rent", 1_45_000_00, date(2026, month, 5)))
        history.append(ScheduledPayout(
            f"V-{month}", "One Off Traders", 20_000_00, date(2026, month, 9)))
    history.append(ScheduledPayout("V-9", "One Off Traders", 90_000_00,
                                   date(2026, 8, 9)))

    found = infer_recurring(history, today=date(2026, 8, 20))
    names = {r.name for r in found}
    assert "Office Rent" in names
    # The wildly varying one is not a recurring charge, it is a coincidence.
    assert "One Off Traders" not in names
    assert 0 < found[0].confidence < 1.0


def test_two_appearances_are_not_a_pattern():
    from merchant.treasury_import import infer_recurring

    twice = [ScheduledPayout(f"R-{m}", "Rent", 1_00_000_00, date(2026, m, 5))
             for m in (6, 7)]
    assert infer_recurring(twice, today=date(2026, 8, 20)) == []


# --- the page -------------------------------------------------------------

@pytest.fixture
def shop(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    return client


def _forecast(shop, source="demo", timeout=30):
    import merchant.app as appmod

    response = shop.post("/agents/cash-forecaster/run",
                         data={"source": source, "use_agent": "no"},
                         follow_redirects=False)
    key = response.headers["location"].split("key=")[-1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        with appmod._cash_lock:
            state = dict(appmod.CASH_RUNS.get(key) or {})
        if state.get("state") != "running":
            return key, state
        time.sleep(0.05)
    raise AssertionError("the forecast never finished")


def test_the_agent_is_registered_as_the_fourth_live_one(shop):
    from merchant.catalog import live_agents

    assert "cash_forecaster" in {a.id for a in live_agents()}
    assert "Forward Cash Forecaster" in shop.get("/agents").text


def test_the_payout_timing_auditor_is_still_planned():
    """
    It measures settlement DELAY and prices the float - a different question.
    Promoting it to ship this would have put one product out under another's
    name.
    """
    from merchant.catalog import get

    assert get("payout_timing").status == "planned"


def test_the_tabs_are_the_three_ways_the_inputs_arrive():
    from merchant.nav import AGENT_ROUTES

    tabs = AGENT_ROUTES["cash_forecaster"].tabs
    assert [t.label for t in tabs] == ["Demo Mode", "Without API", "With API"]


def test_a_demo_run_reaches_the_dashboard(shop):
    key, state = _forecast(shop)
    assert state["state"] == "done", state

    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    # The verdict leads: what is wrong, when, and what to do - in that order.
    assert "You run short on" in page
    assert "What to do" in page
    assert "safe floor" in page
    assert "The days that move" in page


def test_the_page_leads_with_the_decision_not_the_balance(shop):
    """
    Regression on a design defect. The first version led with "in the account
    today", which is the least actionable number here - a controller opening
    this wants to know whether they are fine, when they are not, and what to
    do, in that order.
    """
    key, state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text

    verdict_at = page.index('class="verdict')
    today_at = page.index(state["payload"]["forecast"]["opening_display"])
    assert verdict_at < today_at, "today's balance is leading again"

    # And the date is written for a person, not parsed from an ISO string.
    assert "10 September" in page


def test_the_chart_carries_its_own_scale(shop):
    """
    A dramatic-looking cliff with no vertical axis could be a rupee or a lakh.
    The first version drew the shape and left the reader to infer the numbers.
    """
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text

    assert page.count('class="curve-grid"') >= 3, "no vertical scale"
    assert "curve-floor-line" in page
    # The readout names the low point at rest, and any day on hover.
    assert 'class="curve-read"' in page
    assert page.count('class="curve-hit"') == 30, "one hit column per day"


def test_the_danger_band_has_height(shop):
    """
    The floor used to sit on the bottom edge because the axis was forced to
    zero, which made the danger band - the entire point of the chart - a line
    one pixel tall.
    """
    import re

    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    band = re.search(r'<rect[^>]*fill="var\(--danger\)"[^>]*>', page).group(0)
    height = float(re.search(r'height="([\d.]+)"', band).group(1))
    assert height > 10, f"the danger band is {height}px tall"


def test_the_curve_is_drawn_and_the_danger_zone_marked(shop):
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text

    assert "<polyline" in page
    assert "var(--danger)" in page
    assert "safe floor" in page
    # Thirty points on the line, one per day.
    line = page.split('<polyline points="')[1].split('"')[0]
    assert len(line.split()) == 30


def test_the_alert_names_the_date_and_the_shortfall(shop):
    key, state = _forecast(shop)
    trough = state["payload"]["forecast"]["trough"]
    page = shop.get(f"/agents/cash-forecaster?key={key}").text

    assert "finding-card" in page
    assert trough["balance_display"] in page
    assert trough["shortfall_display"] in page
    assert "Cannot be moved" in page


def test_upload_needs_a_balance_and_something_to_spend(shop):
    page = shop.get("/agents/cash-forecaster/upload").text
    assert "Not ready yet" in page

    _key, state = _forecast(shop, source="upload")
    assert state["state"] == "failed"
    assert "needs a starting balance" in state["phase"]


def test_uploaded_files_produce_the_same_shape_of_forecast(shop):
    """
    The convergence requirement: demo, upload and connected all become a
    TreasuryInputs and nothing downstream can tell which.
    """
    shop.post("/agents/cash-forecaster/upload", data={"kind": "account"},
              files={"balances": ("bal.csv",
                                  b"Bank,Balance,As Of\n"
                                  b"HDFC current,705000,27-08-2026\n",
                                  "text/csv")})
    shop.post("/agents/cash-forecaster/upload", data={"kind": "payout"},
              files={"payouts": ("ap.csv",
                                 b"Reference,Vendor,Amount,Due Date\n"
                                 b"PAY-1,Payroll August,620000,10-09-2026\n"
                                 b"V-77,Sundaram Packaging,110000,09-09-2026\n",
                                 "text/csv")})

    key, state = _forecast(shop, source="upload")
    assert state["state"] == "done", state

    payload = state["payload"]
    assert payload["metadata"]["source"] == "upload"
    assert len(payload["forecast"]["positions"]) == 30
    # Real inputs have no planted scenario, so nothing is claimed about them.
    assert payload["metadata"]["accuracy"] == {}

    page = shop.get(f"/agents/cash-forecaster/upload?key={key}").text
    assert "<polyline" in page


def test_payroll_uploaded_by_name_is_marked_unmovable(shop):
    """
    The failure this guards is advising somebody to delay a salary. The kind
    is inferred from the payee where a file does not say.
    """
    response = shop.post(
        "/agents/cash-forecaster/upload", data={"kind": "payout"},
        files={"payouts": ("ap.csv",
                           b"Reference,Vendor,Amount,Due Date\n"
                           b"PAY-1,Payroll August,620000,10-09-2026\n"
                           b"V-77,Sundaram Packaging,110000,09-09-2026\n",
                           "text/csv")})
    assert "marked unmovable" in response.text

    # A balance too - treasury_inputs correctly returns None without one.
    shop.post("/agents/cash-forecaster/upload", data={"kind": "account"},
              files={"balances": ("bal.csv",
                                  b"Bank,Balance,As Of\n"
                                  b"HDFC current,705000,27-08-2026\n",
                                  "text/csv")})

    import merchant.app as appmod

    with appmod.ledger(None) as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        inputs = led.treasury_inputs()
    payroll = [p for p in inputs.payouts if p.payout_id == "PAY-1"][0]
    assert payroll.movable is False


def test_a_payout_with_no_due_date_is_dropped_and_named(shop):
    """A forecast is a statement about dates. An obligation with no date
    cannot be placed on the curve, and guessing today would invent a trough."""
    from merchant.treasury_import import parse_payouts

    result = parse_payouts(b"Reference,Vendor,Amount,Due Date\n"
                           b"V-1,Someone,1000,not-a-date\n", "ap.csv")
    assert result.payouts == []
    assert "no readable due date" in result.rows_skipped[0]


def test_the_connected_tab_is_honest_about_what_it_fixes(shop):
    page = shop.get("/agents/cash-forecaster/connected").text
    assert "No Razorpay account is connected" in page
    # And says the balance stays an upload, because no bank API reaches it.
    assert "balance" in page.lower()


def test_the_json_payload_is_available_on_its_own(shop):
    key, _state = _forecast(shop)
    payload = shop.get(f"/agents/cash-forecaster/{key}.json").json()

    assert payload["forecast"]["trough"]["day"] == CRUNCH_DAY
    assert payload["metadata"]["accuracy"]["all_passed"] is True


def test_an_unknown_run_is_a_404(shop):
    assert shop.get("/agents/cash-forecaster/nope.json").status_code == 404


def test_only_the_line_turns_red_not_the_whole_month(shop):
    """
    Filling thirty days in danger colour because one of them is bad reads as
    "this whole month is a crisis" - untrue, and the fastest way to teach
    somebody to ignore the colour.
    """
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text

    svg = page.split('<div class="curve-plot"')[1].split("</svg>")[0]
    area = svg.split("<polygon")[1].split(">")[0]
    line = svg.split("<polyline")[1].split(">")[0]

    assert "var(--brand)" in area, "the area under the curve should stay calm"
    assert "var(--danger)" in line, "the line itself marks the breach"


def test_the_axis_does_not_invent_negative_territory(shop):
    """
    Padding the bottom by a share of the whole span put the axis at minus one
    and a third lakh on a month that never goes negative - a chunk of empty
    chart labelled with a number that cannot happen.
    """
    key, state = _forecast(shop)
    assert state["payload"]["forecast"]["trough"]["below_zero"] is False

    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    labels = page.split('class="curve-grid"')[1:]
    assert labels
    assert not any("Rs -" in label.split("</div>")[0] for label in labels)


def test_the_card_does_not_restate_the_verdict(shop):
    """
    Without the agent the card repeated the headline sentence word for word,
    so the same figures were read twice and neither said anything new.
    """
    key, state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    detail = state["payload"]["forecast"]["detail"]

    card = page.split('class="finding-card"')[1]
    assert detail not in card


def test_the_chart_answers_about_any_day_not_only_the_worst(shop):
    """
    A cash curve is thirty numbers. The still version showed one of them and
    drew the other twenty-nine, so a controller could see the shape of the
    month and read exactly one figure off it.
    """
    key, state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text

    hits = page.count('class="curve-hit"')
    assert hits == len(state["payload"]["forecast"]["positions"])

    # Every figure is server-rendered into the DOM; the browser picks one to
    # show and computes nothing, which is the engine's rule applied to the
    # front end.
    assert 'data-bal="Rs ' in page
    assert "data-in=" in page and "data-out=" in page
    assert "computes nothing" in page or "curve-cross" in page


def test_the_readout_falls_back_to_the_low_point(shop):
    """Without JavaScript the chart still draws and the low point is still
    named - the interaction adds to a complete answer."""
    key, state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    low = state["payload"]["forecast"]["trough"]

    curve = page.split('<div class="curve"')[1]
    assert f'data-low="{low["day"] - 1}"' in curve
