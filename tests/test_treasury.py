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


def test_the_cash_forecaster_and_payout_timing_stay_distinct_agents():
    """
    Cash forecasting projects a balance forward; payout timing measures
    settlement DELAY and prices the float - a different question, now a
    different live agent rather than one product folded under another's
    name.
    """
    from merchant.catalog import get

    assert get("payout_timing") is not None
    assert get("payout_timing").status == "live"
    assert get("payout_timing").id != get("cash_forecaster").id


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
    # And both axes are named, not left to be inferred.
    assert "curve-ylabel" in page and "curve-xlabel" in page
    assert page.count('class="curve-hit"') == 30, "one hit column per day"


def test_the_area_below_the_floor_has_height(shop):
    """
    The floor used to sit on the bottom edge because the axis was forced to
    zero, which made everything below it - the entire point of the chart - a
    strip one pixel tall.
    """
    import re

    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    clip = re.search(r'-under">\s*<rect[^>]*>', page).group(0)
    height = float(re.search(r'height="([\d.]+)"', clip).group(1))
    assert height > 10, f"the area below the floor is {height}px tall"


def test_the_curve_is_drawn_and_the_danger_zone_marked(shop):
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text

    assert "var(--danger)" in page
    assert "safe floor" in page
    # One dot per day, so the data points are visible without hovering.
    assert page.count('class="curve-pt') == 30


def test_the_line_passes_through_every_day(shop):
    """
    The spline is clamped so it goes through each point rather than near it.

    An unclamped smoothing overshoots at a sharp turn, so the cliff on the day
    payroll lands would dip below the balance actually reached - a chart
    drawing a trough that did not happen, in a product whose whole claim is
    that the arithmetic is exact.
    """
    from merchant.views import _spline

    points = [(0.0, 10.0), (1.0, 10.0), (2.0, 0.0), (3.0, 10.0), (4.0, 10.0)]
    path = _spline(points)

    # Every data point appears as an endpoint of a segment.
    for x, y in points[1:]:
        assert f"{x:.1f},{y:.1f}" in path

    # And no control point dips below the lowest data point.
    import re

    ys = [float(v) for v in re.findall(r"[-\d.]+,([-\d.]+)", path)]
    assert max(ys) <= 10.0 + 0.001, "the spline overshoots past the data"


def test_the_fill_changes_colour_at_the_floor(shop):
    """
    The band sits between the line and the SAFE FLOOR, clipped at it - so a
    day above the floor is tinted calm and the stretch below is red, in one
    glance rather than by reading an axis.
    """
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text

    assert "clipPath" in page
    assert page.count("-over)") >= 2 and page.count("-under)") >= 2
    # And the line keeps an even weight when the plot is stretched sideways.
    assert page.count('vector-effect="non-scaling-stroke"') == 2


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
    assert 'class="curve-plot"' in page


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


def test_the_calm_stretch_is_not_painted_as_a_crisis(shop):
    """
    Filling thirty days in danger colour because one of them is bad reads as
    "this whole month is a crisis" - untrue, and the fastest way to teach
    somebody to ignore the colour. Only what is below the floor is red.
    """
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    svg = page.split('<div class="curve-plot"')[1].split("</svg>")[0]

    assert 'fill="var(--brand)"' in svg, "the calm stretch should stay calm"
    assert 'fill="var(--danger)"' in svg, "the breach should be marked"
    # Most days are above the floor, so most dots are the calm colour.
    calm = page.count('class="curve-pt"') + page.count('class="curve-pt low"')
    assert calm > page.count("curve-pt under")


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
    assert 'class="curve-tip"' in page


def test_the_readout_falls_back_to_the_low_point(shop):
    """Without JavaScript the chart still draws and the low point is still
    named - the interaction adds to a complete answer."""
    key, state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    low = state["payload"]["forecast"]["trough"]

    curve = page.split('<div class="curve"')[1]
    assert f'data-low="{low["day"] - 1}"' in curve
    # The low point is drawn differently, so it is findable with the pointer
    # nowhere near the chart.
    assert 'class="curve-pt under low"' in page or 'curve-pt low' in page


def test_the_axis_labels_do_not_sit_on_the_line(shop):
    """
    They used to be at left:0 inside the plot, so the first day of the curve
    ran straight through "Rs 8L".
    """
    from merchant.views import CSS

    assert "margin-left:46px" in CSS
    label = CSS.split(".curve-grid span {")[1].split("}")[0]
    assert "right:100%" in label, "the labels are back inside the plot"


# --- what is earned, and what is assumed ----------------------------------
#
# Every one of the thirty days is a projection, so shading "the forecast part"
# would mean inventing a boundary. This one is real: up to it the incoming
# money is settlements from payments already taken and invoices already
# raised; past it the receipts assume trade carries on.

def test_the_earned_horizon_comes_from_the_receipts_not_a_guess():
    """
    A merchant with B2B invoices out has earned receivables weeks ahead; one
    taking only card payments has about two days of them. So it is computed
    from what is actually on the books.
    """
    from engine.treasury.records import ExpectedReceipt

    cards_only = _inputs(receipts=[
        ExpectedReceipt("s1", "gateway settlement", 10_000_00,
                        TODAY + timedelta(days=2), certain=True),
        ExpectedReceipt("s2", "gateway settlement", 10_000_00,
                        TODAY + timedelta(days=20), certain=False)])
    assert project_cash_flow(cards_only).earned_through_day == 2

    with_invoices = _inputs(receipts=[
        ExpectedReceipt("s1", "gateway settlement", 10_000_00,
                        TODAY + timedelta(days=2), certain=True),
        ExpectedReceipt("INV-1", "customer invoice", 5_00_000_00,
                        TODAY + timedelta(days=21), certain=True)])
    assert project_cash_flow(with_invoices).earned_through_day == 21


def test_assumed_receipts_are_counted_separately():
    forecast = project_cash_flow(generate(as_of=TODAY)[0])
    payload = forecast.as_dict()

    assert payload["earned_through_day"] == 16
    assert payload["assumed_receipts"] > 0
    # And it is only what lands past the horizon.
    beyond = sum(p.receipts for p in forecast.positions
                 if p.day > forecast.earned_through_day)
    assert payload["assumed_receipts"] == beyond


def test_the_demo_relief_is_earned_not_assumed():
    """
    The two receipts that cover the crunch are invoices already raised. If
    they were assumed, the scenario would be a hope rather than a scheduling
    problem, and the recommendation would be dishonest.
    """
    inputs, planted = generate(as_of=TODAY)
    relief = [r for r in inputs.receipts
              if r.expected_on == TODAY + timedelta(
                  days=planted["relief_lands_on_day"])]

    assert relief
    assert all(r.certain for r in relief)
    assert sum(r.amount for r in relief) >= 3_00_000_00


def test_the_chart_marks_where_earned_money_stops(shop):
    key, state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    earned = state["payload"]["forecast"]["earned_through_day"]

    assert "curve-assumed" in page
    assert "earned to here" in page
    assert f"past day {earned}" in page
    assert "assumes trade carries on" in page


def test_nothing_is_shaded_when_everything_is_earned():
    """
    A merchant whose whole month is invoiced has no assumed stretch, and a
    band covering nothing would be furniture.
    """
    from engine.treasury.records import ExpectedReceipt

    from merchant.views import cash_curve

    all_earned = _inputs(receipts=[
        ExpectedReceipt("INV-1", "customer invoice", 10_000_00,
                        TODAY + timedelta(days=30), certain=True)])
    html = cash_curve(project_cash_flow(all_earned).as_dict())
    assert "curve-assumed" not in html


# --- the agent can check itself now ---------------------------------------
#
# It used to be handed a list of movable payouts and asked to pick one, with
# nothing able to tell whether the one it picked would work. These tools are
# the difference between a classifier with a narration layer and an agent.

def _tools(inputs=None):
    from agent.treasury_tools import build_tools

    inputs = inputs or generate(as_of=TODAY)[0]
    return inputs, {t.name: t for t in build_tools(
        inputs, project_cash_flow(inputs))}


def _call(tools, name, **kw):
    import json

    return json.loads(tools[name].call(kw))


def test_the_tools_are_all_read_only():
    """
    Guardrail 1 is enforced by never giving the agent a tool that can write,
    not by asking it nicely in a prompt. what_if_delayed SIMULATES against a
    copy; nothing it does survives the call.
    """
    inputs, tools = _tools()
    assert set(tools) == {"what_if_delayed", "payout_detail", "movements_on"}

    # Asserted as a PROPERTY, not by looking at the names. The first version
    # of this test rejected any tool starting with "move" and failed on
    # movements_on, which reads a day and changes nothing - a string check
    # wearing a safety check's clothes.
    before = ([(p.payout_id, p.due_on, p.amount) for p in inputs.payouts],
              [(a.account_id, a.balance) for a in inputs.accounts],
              [(r.reference, r.expected_on, r.amount) for r in inputs.receipts])

    _call(tools, "what_if_delayed", payout_id="V-1042", days_later=7)
    _call(tools, "payout_detail", payout_id="V-1042")
    _call(tools, "movements_on", day=14)

    after = ([(p.payout_id, p.due_on, p.amount) for p in inputs.payouts],
             [(a.account_id, a.balance) for a in inputs.accounts],
             [(r.reference, r.expected_on, r.amount) for r in inputs.receipts])
    assert before == after, "a tool changed the merchant's data"


def test_what_if_delayed_answers_the_question_the_engine_could_not():
    """
    The engine says 'Rs 4,05,000 could move and you are Rs 43,311 short'. It
    cannot say whether moving any PARTICULAR one works. Now something can.
    """
    _inputs_, tools = _tools()

    helps = _call(tools, "what_if_delayed", payout_id="V-1042", days_later=3)
    assert helps["clears_the_floor"] is True
    assert "clears the floor" in helps["verdict"]

    useless = _call(tools, "what_if_delayed", payout_id="V-1051", days_later=3)
    assert useless["clears_the_floor"] is False
    assert "still short" in useless["verdict"]


def test_it_refuses_to_model_delaying_a_salary():
    """
    That it is 'only a simulation' is not a defence. Its output becomes a
    recommendation, and a recommendation to defer payroll is a recommendation
    to default.
    """
    _inputs_, tools = _tools()
    for pid in ("PAY-PAYROLL-08", "PAY-TDS-Q2"):
        out = _call(tools, "what_if_delayed", payout_id=pid, days_later=5)
        assert out["refused"] is True
        assert "clears_the_floor" not in out


def test_a_delay_is_capped_at_what_the_payout_allows():
    _inputs_, tools = _tools()
    out = _call(tools, "what_if_delayed", payout_id="V-1042", days_later=99)
    assert out["capped"] is True
    assert out["moved_by_days"] == out["furthest_it_can_move_days"]


def test_an_unknown_payout_says_so_rather_than_guessing():
    _inputs_, tools = _tools()
    out = _call(tools, "what_if_delayed", payout_id="V-9999", days_later=3)
    assert "error" in out
    assert out["known_ids"]


def test_simulating_does_not_change_the_real_forecast():
    """
    A tool that quietly rewrote the forecast it was asked about would make the
    second question return a different answer from the first for no visible
    reason.
    """
    inputs, tools = _tools()
    before = project_cash_flow(inputs).trough.balance
    for pid in ("V-1042", "V-1051"):
        _call(tools, "what_if_delayed", payout_id=pid, days_later=7)
    assert project_cash_flow(inputs).trough.balance == before


def test_the_tool_catches_a_move_that_creates_a_new_shortfall():
    """
    The reason this beats a filter on the movable list.

    A payout moved off the low point has to land somewhere, and it can push
    a later day under instead. Only re-running the projection finds that.
    """
    from engine.treasury.records import (KIND_VENDOR, BankAccount,
                                         ScheduledPayout, TreasuryInputs)

    # Two tight days. Moving the first payout onto the second sinks it.
    inputs = TreasuryInputs(
        accounts=[BankAccount("acc", "Test", 2_00_000_00, TODAY)],
        payouts=[
            ScheduledPayout("V-A", "Vendor A", 90_000_00,
                            TODAY + timedelta(days=4), KIND_VENDOR),
            ScheduledPayout("V-B", "Vendor B", 90_000_00,
                            TODAY + timedelta(days=6), KIND_VENDOR)],
        as_of=TODAY)
    _inputs_, tools = _tools(inputs)

    moved = _call(tools, "what_if_delayed", payout_id="V-A", days_later=2)
    assert moved["clears_the_floor"] is False
    assert moved["low_point_after"]["day"] == 6


def test_movements_on_opens_one_day():
    _inputs_, tools = _tools()
    out = _call(tools, "movements_on", day=14)
    assert out["below_the_floor"] is True
    assert out["out_detail"]
    assert _call(tools, "movements_on", day=99)["error"]


def test_payout_detail_says_why_something_cannot_move():
    _inputs_, tools = _tools()
    fixed = _call(tools, "payout_detail", payout_id="PAY-PAYROLL-08")
    assert fixed["movable"] is False
    assert fixed["why_fixed"]
    assert _call(tools, "payout_detail", payout_id="V-1042")["movable"] is True


def test_the_agent_still_answers_without_tools():
    """
    Tools are an addition to a complete answer, never a precondition for one.
    Without inputs the agent behaves exactly as it did before they existed.
    """
    from agent.treasury_classifier import ClaudeTreasuryAgent

    class Refuses:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**kw):
                    assert kw["tools"] == [], "no inputs means no tools"
                    raise ConnectionError("down")

    forecast = project_cash_flow(generate(as_of=TODAY)[0])
    verdict = ClaudeTreasuryAgent(client=Refuses()).judge(forecast)
    assert verdict.action == forecast.action
    assert verdict.tool_calls == []


def test_what_it_checked_is_shown_to_the_merchant(shop):
    """
    An agent that checked three candidates and one that picked the first name
    on a list produce identical prose. Only one deserves to be believed, so
    the page has to be able to tell them apart.
    """
    import merchant.app as appmod

    key, state = _forecast(shop)
    payload = state["payload"]
    payload["verdict"] = {
        "action": payload["forecast"]["action"], "confidence": 0.9,
        "reasoning": "Hold V-1042 for three days.",
        "hold_payout_id": "V-1042", "hold_days": 3,
        "tool_calls": ["what_if_delayed", "what_if_delayed", "payout_detail"],
        "corrections": [], "errored": False,
    }
    with appmod._cash_lock:
        appmod.CASH_RUNS[key]["payload"] = payload

    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    assert "Before deciding, it checked" in page
    assert "simulated moving a payment" in page
    assert "&times;2" in page


def test_a_disputed_receipt_becomes_a_clickable_link(shop):
    """
    The one live cross-agent connection, made visible rather than left as a
    sentence buried in the agent's prose. A merchant reading this page should
    be able to open the actual settlement finding, not just be told about it.
    """
    import merchant.app as appmod

    key, state = _forecast(shop)
    payload = state["payload"]
    payload["verdict"] = {
        "action": payload["forecast"]["action"], "confidence": 0.9,
        "reasoning": "Do not count on the relief receipt.",
        "hold_payout_id": None, "hold_days": None,
        "tool_calls": ["settlement_status"], "corrections": [], "errored": False,
        "disputed_receipts": [{
            "payment_id": "pay_9K3fL2xQ1z", "run_id": "run_abcd1234",
            "exception_code": "ZERO_MDR_VIOLATION", "money_at_stake": 200000,
            "money_at_stake_display": "Rs 2,000.00",
        }],
    }
    with appmod._cash_lock:
        appmod.CASH_RUNS[key]["payload"] = payload

    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    assert "pay_9K3fL2xQ1z" in page
    assert "zero mdr violation" in page
    assert "Rs 2,000.00" in page
    assert 'href="/agents/settlement/run/run_abcd1234"' in page
    assert "your settlement audit already found a problem" in page


def test_no_dispute_means_no_extra_banner(shop):
    """The common case - nothing to link to, nothing extra shown."""
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    assert "your settlement audit already found a problem" not in page


def test_at_risk_credit_becomes_a_clickable_link(shop):
    """
    The second live cross-agent connection: claimed GST input credit at risk
    of being clawed back, surfaced the same way the disputed receipt is -
    a link into the agent that actually found it, not a sentence about it.
    """
    import merchant.app as appmod

    key, state = _forecast(shop)
    payload = state["payload"]
    payload["verdict"] = {
        "action": payload["forecast"]["action"], "confidence": 0.9,
        "reasoning": "Watch the claimed credit at risk this quarter.",
        "hold_payout_id": None, "hold_days": None,
        "tool_calls": ["at_risk_input_credit"], "corrections": [],
        "errored": False,
        "at_risk_credit": {"at_risk_paise": 500000,
                           "at_risk_display": "Rs 5,000.00", "count": 2},
    }
    with appmod._cash_lock:
        appmod.CASH_RUNS[key]["payload"] = payload

    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    assert "Rs 5,000.00" in page
    assert "2 claims" in page
    assert 'href="/agents/input-credit/reconciliation"' in page
    assert "may have to be repaid" in page


def test_no_at_risk_credit_means_no_extra_banner(shop):
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    assert "may have to be repaid" not in page


def test_a_healthy_month_still_shows_at_risk_credit(shop):
    """A comfortable balance that includes money owed back is not as
    comfortable as it looks - this has to survive the CASH_HEALTHY branch,
    which returns early and does not otherwise look at the verdict at all."""
    import merchant.app as appmod

    key, state = _forecast(shop)
    payload = state["payload"]
    payload["forecast"]["finding_type"] = "CASH_HEALTHY"
    payload["verdict"] = {
        "action": "none", "confidence": 0.9, "reasoning": "Comfortable.",
        "hold_payout_id": None, "hold_days": None, "tool_calls": [],
        "corrections": [], "errored": False,
        "at_risk_credit": {"at_risk_paise": 500000,
                           "at_risk_display": "Rs 5,000.00", "count": 1},
    }
    with appmod._cash_lock:
        appmod.CASH_RUNS[key]["payload"] = payload

    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    assert "may have to be repaid" in page


def test_a_recon_flag_becomes_a_clickable_link(shop):
    """
    The third live cross-agent connection: a receipt this forecast is
    counting on that the three-way reconciler already flagged as never
    credited - same treatment as the other two connections.
    """
    import merchant.app as appmod

    key, state = _forecast(shop)
    payload = state["payload"]
    payload["verdict"] = {
        "action": payload["forecast"]["action"], "confidence": 0.9,
        "reasoning": "Do not count on the flagged receipt.",
        "hold_payout_id": None, "hold_days": None,
        "tool_calls": ["recon_status"], "corrections": [], "errored": False,
        "recon_flagged": [{
            "payment_id": "pay_9K3fL2xQ1z", "run_id": "recon_abcd1234",
            "finding": "MISSING_IN_BANK", "at_stake": 488200,
            "at_stake_display": "Rs 4,882.00",
        }],
    }
    with appmod._cash_lock:
        appmod.CASH_RUNS[key]["payload"] = payload

    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    assert "pay_9K3fL2xQ1z" in page
    assert "missing in bank" in page
    assert "Rs 4,882.00" in page
    assert 'href="/agents/three-way"' in page
    assert "your three-way reconciliation already flagged it" in page


def test_no_recon_flag_means_no_extra_banner(shop):
    key, _state = _forecast(shop)
    page = shop.get(f"/agents/cash-forecaster?key={key}").text
    assert "your three-way reconciliation already flagged it" not in page


def test_a_tool_run_reports_what_it_actually_cost():
    """
    Regression, and it was live in the settlement agent too.

    Each message in a tool loop reports only its own turn. Reading usage off
    the last one showed "2 input tokens" for a conversation that had run four
    turns and re-sent the evidence every time - so the "what this run cost"
    figure on the page, which exists precisely because somebody is paying for
    it, was wrong for every run that used a tool.
    """
    from agent.treasury_classifier import ClaudeTreasuryAgent

    from agent.treasury_classifier import TreasuryJudgment

    def _judgment():
        return TreasuryJudgment(exception_code="CASH_CRUNCH_WARNING",
                                action="delay_payout", confidence=0.9,
                                reasoning="Hold it.")

    class Turn:
        def __init__(self, i, o, last=False):
            self.usage = type("U", (), {"input_tokens": i, "output_tokens": o,
                                        "cache_read_input_tokens": 0})()
            self.content = []
            self.parsed_output = _judgment() if last else None

    turns = [Turn(1000, 200), Turn(1200, 150), Turn(1400, 300, last=True)]

    class Runner:
        def __iter__(self):
            return iter(turns)

        def generate_tool_call_response(self):
            return None

    class Client:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**_kw):
                    return Runner()

    forecast = project_cash_flow(generate(as_of=TODAY)[0])
    verdict = ClaudeTreasuryAgent(client=Client()).judge(forecast)

    assert verdict.input_tokens == 3600, "only the last turn was counted"
    assert verdict.output_tokens == 650


def test_extra_tools_reach_the_model_alongside_the_built_in_ones():
    """
    The cross-agent settlement lookup is built outside this module (it needs
    database access this layer deliberately does not have - see
    merchant/cross_agent_tools.py) and handed in as `extra_tools`. Confirm it
    actually reaches the tool_runner call rather than being dropped.
    """
    from agent.treasury_classifier import ClaudeTreasuryAgent

    seen = {}

    class Captures:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**kw):
                    seen["tools"] = kw["tools"]
                    raise ConnectionError("down")

    def fake_settlement_status():
        pass
    fake_settlement_status.name = "settlement_status"

    forecast = project_cash_flow(generate(as_of=TODAY)[0])
    ClaudeTreasuryAgent(client=Captures()).judge(
        forecast, extra_tools=[fake_settlement_status])

    names = [getattr(t, "name", t) for t in seen["tools"]]
    assert "settlement_status" in names


def test_a_demo_run_never_gets_the_cross_agent_tool():
    """
    A demo forecast's payment ids were generated fresh for that scenario -
    nothing in the settlement tables has ever heard of them. Offering the
    tool there would only ever answer "nothing found", which is an artifact
    of the demo data, not a checked fact. See cross_agent_tools.build_tools().
    """
    seen = {}

    class FakeAgent:
        def judge(self, forecast, business="", inputs=None, extra_tools=None):
            seen["extra_tools"] = extra_tools
            from agent.treasury_classifier import TreasuryVerdict

            return TreasuryVerdict(exception_code=forecast.finding,
                                   action=forecast.action, confidence=0.9,
                                   reasoning="ok")

    inputs, planted = generate(as_of=TODAY)
    run(inputs, agent=FakeAgent(), source="demo", business_id="biz_1",
        planted=planted)

    assert seen["extra_tools"] == []


def test_a_connected_run_is_offered_the_cross_agent_tool():
    seen = {}

    class FakeAgent:
        def judge(self, forecast, business="", inputs=None, extra_tools=None):
            seen["extra_tools"] = extra_tools
            from agent.treasury_classifier import TreasuryVerdict

            return TreasuryVerdict(exception_code=forecast.finding,
                                   action=forecast.action, confidence=0.9,
                                   reasoning="ok")

    inputs, _ = generate(as_of=TODAY)
    run(inputs, agent=FakeAgent(), source="connected", business_id="biz_1")

    names = {t.name for t in seen["extra_tools"]}
    assert names == {"settlement_status", "at_risk_input_credit",
                     "recon_status", "at_risk_output_tax"}


def test_a_connected_run_with_no_business_gets_no_extra_tool():
    """Belt and braces: the tool needs a business to scope its query to, and
    without one the pipeline should not try to build it at all."""
    seen = {}

    class FakeAgent:
        def judge(self, forecast, business="", inputs=None, extra_tools=None):
            seen["extra_tools"] = extra_tools
            from agent.treasury_classifier import TreasuryVerdict

            return TreasuryVerdict(exception_code=forecast.finding,
                                   action=forecast.action, confidence=0.9,
                                   reasoning="ok")

    inputs, _ = generate(as_of=TODAY)
    run(inputs, agent=FakeAgent(), source="connected", business_id="")

    assert seen["extra_tools"] == []


def test_a_figure_from_a_tool_is_not_flagged_as_invented():
    """
    Same fix as the settlement agent's classifier.py, and the same reason:
    the cross-agent tool can tell the model a relief receipt is under dispute
    for a specific rupee figure, and that figure was computed by our own
    Python - not made up. Checking the model's reasoning only against the
    static evidence text would flag it as invented and throw the whole
    recommendation away, exactly as it did live before this fix.
    """
    from agent.treasury_classifier import ClaudeTreasuryAgent, TreasuryJudgment

    tool_json = ('{"payment_id": "pay_x", "at_risk":'
                ' {"paise": 200000, "display": "Rs 2,000.00"}}')

    def _judgment():
        return TreasuryJudgment(
            exception_code="CASH_CRUNCH_WARNING", action="delay_payout",
            confidence=0.9, hold_payout_id="V-1042", hold_days=4,
            reasoning="Hold V-1042. One relief receipt is under dispute for "
                      "Rs 2,000.00, so do not count on all of it arriving.")

    class ToolUseBlock:
        type = "tool_use"
        name = "settlement_status"

    class Turn:
        def __init__(self, content=None, last=False):
            self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 10,
                                        "cache_read_input_tokens": 0})()
            self.content = content or []
            self.parsed_output = _judgment() if last else None

    turns = [Turn([ToolUseBlock()]), Turn([], last=True)]

    class Runner:
        def __init__(self):
            self._served = False

        def __iter__(self):
            return iter(turns)

        def generate_tool_call_response(self):
            if not self._served:
                self._served = True
                return {"content": [{"type": "tool_result",
                                    "content": tool_json}]}
            return None

    class Client:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**_kw):
                    return Runner()

    forecast = project_cash_flow(generate(as_of=TODAY)[0])
    verdict = ClaudeTreasuryAgent(client=Client()).judge(forecast)

    assert verdict.corrections == [], (
        "a figure the tool supplied was wrongly treated as invented")
    assert "under dispute" in verdict.reasoning


def test_a_found_dispute_is_attached_to_the_verdict(tmp_path, monkeypatch):
    """
    The whole reason the `found` accumulator exists: after judge() calls the
    tool and returns, the pipeline has to read back what it found and attach
    it to the verdict, or the merchant never sees a link to the actual
    settlement finding - only a sentence in the agent's prose, which is where
    this connection stood before it was made clickable.
    """
    import json as _json

    import merchant.cross_agent_tools as cat
    from merchant.ledger import Ledger

    db = tmp_path / "found.db"
    monkeypatch.setattr(cat, "DB", str(db))

    bootstrap = Ledger(db)
    business_id = bootstrap.businesses.create("Test Co")
    bootstrap.close()

    led = Ledger(db, business_id)
    led.conn.execute(
        "INSERT INTO business_runs (run_id, business_id, created_at)"
        " VALUES ('run_disputed', ?, 0)", (business_id,))
    led.conn.execute(
        "INSERT INTO variances (payment_id, run_id, expected_fee, actual_fee,"
        " expected_tax, actual_tax, delta, money_at_stake, exception_code,"
        " confidence, reasoning, rule_cited, action, human_reviewed,"
        " queued_for_human, created_at)"
        " VALUES ('pay_relied_on', 'run_disputed', 0, 6100, 0, 1098, 7198,"
        " 200000, 'ZERO_MDR_VIOLATION', 0.9, 'network MDR on UPI',"
        " 'PSS Act s.10A', 'dispute', 0, 0, 0)")
    led.conn.commit()
    led.close()

    class FakeAgent:
        def judge(self, forecast, business="", inputs=None, extra_tools=None):
            for tool in extra_tools:
                if tool.name == "settlement_status":
                    tool.call({"payment_id": "pay_relied_on"})
            from agent.treasury_classifier import TreasuryVerdict

            return TreasuryVerdict(exception_code=forecast.finding,
                                   action=forecast.action, confidence=0.9,
                                   reasoning="ok")

    inputs, _ = generate(as_of=TODAY)
    result = run(inputs, agent=FakeAgent(), source="connected",
                 business_id=business_id)

    receipts = result.verdict.disputed_receipts
    assert len(receipts) == 1
    assert receipts[0]["payment_id"] == "pay_relied_on"
    assert receipts[0]["run_id"] == "run_disputed"
    assert receipts[0]["money_at_stake"] == 200000

    # And it round-trips into the payload the page actually reads.
    payload = _json.loads(_json.dumps(result.as_dict()))
    assert payload["verdict"]["disputed_receipts"][0]["run_id"] == "run_disputed"


def test_found_at_risk_credit_is_attached_to_the_verdict(tmp_path, monkeypatch):
    """Same wiring, the other cross-agent connection: claimed input credit
    the GST reconciler found should not have been claimed."""
    import json as _json

    import merchant.cross_agent_tools as cat
    from merchant.ledger import Ledger

    db = tmp_path / "found_credit.db"
    monkeypatch.setattr(cat, "DB", str(db))

    bootstrap = Ledger(db)
    business_id = bootstrap.businesses.create("Test Co")
    bootstrap.close()

    led = Ledger(db, business_id)
    led.conn.execute(
        "INSERT INTO business_itc_runs (run_id, business_id, period,"
        " n_invoices, created_at) VALUES ('itc_run', ?, '2026-08', 1, 0)",
        (business_id,))
    led.conn.execute(
        "INSERT INTO itc_findings (run_id, business_id, invoice_id,"
        " supplier_name, supplier_gstin, invoice_number, invoice_date,"
        " taxable_value, cgst, sgst, igst, claimed_tax, available_tax, delta,"
        " tolerance, exception_code, action, confidence, reasoning,"
        " rule_cited, decided_by, money_at_stake, queued_for_human,"
        " claim_deadline, days_to_deadline, created_at)"
        " VALUES ('itc_run', ?, 'inv_1', 'Sundaram Packaging',"
        " '29ABCDE1234F1Z5', 'INV-1001', '2026-08-01', 5000, 0, 0, 5000,"
        " 5000, 5000, 0, 0, 'BLOCKED_CREDIT', 'do_not_claim', 0.9,"
        " 'blocked credit', 's.17(5)', 'calculator', 5000, 0,"
        " '2026-11-30', 30, 0)", (business_id,))
    led.conn.commit()
    led.close()

    class FakeAgent:
        def judge(self, forecast, business="", inputs=None, extra_tools=None):
            for tool in extra_tools:
                if tool.name == "at_risk_input_credit":
                    tool.call({})
            from agent.treasury_classifier import TreasuryVerdict

            return TreasuryVerdict(exception_code=forecast.finding,
                                   action=forecast.action, confidence=0.9,
                                   reasoning="ok")

    inputs, _ = generate(as_of=TODAY)
    result = run(inputs, agent=FakeAgent(), source="connected",
                 business_id=business_id)

    assert result.verdict.at_risk_credit["at_risk_paise"] == 5000
    assert result.verdict.at_risk_credit["count"] == 1

    payload = _json.loads(_json.dumps(result.as_dict()))
    assert payload["verdict"]["at_risk_credit"]["at_risk_paise"] == 5000


def test_no_tool_call_leaves_at_risk_credit_empty():
    """The common case - the agent never called the tool, or found nothing.
    An empty dict, not a missing key, so the page can check it uniformly."""
    class FakeAgent:
        def judge(self, forecast, business="", inputs=None, extra_tools=None):
            from agent.treasury_classifier import TreasuryVerdict

            return TreasuryVerdict(exception_code=forecast.finding,
                                   action=forecast.action, confidence=0.9,
                                   reasoning="ok")

    inputs, _ = generate(as_of=TODAY)
    result = run(inputs, agent=FakeAgent(), source="demo")
    assert result.verdict.at_risk_credit == {}


def test_found_recon_flag_is_attached_to_the_verdict(tmp_path, monkeypatch):
    """Third cross-agent connection: a receipt this run is counting on that
    the three-way reconciler already flagged as never credited."""
    import json as _json

    import merchant.cross_agent_tools as cat
    from merchant.ledger import Ledger

    db = tmp_path / "found_recon.db"
    monkeypatch.setattr(cat, "DB", str(db))

    bootstrap = Ledger(db)
    business_id = bootstrap.businesses.create("Test Co")
    bootstrap.close()

    led = Ledger(db, business_id)
    led.conn.execute(
        "INSERT INTO business_recon_runs (run_id, business_id, source,"
        " n_records, created_at) VALUES ('recon_run', ?, 'connected', 1, 0)",
        (business_id,))
    led.conn.execute(
        "INSERT INTO recon_findings (run_id, business_id, invoice_id,"
        " txn_id, utr_number, finding, variance, at_stake, action,"
        " reasoning, detail, created_at)"
        " VALUES ('recon_run', ?, 'INV-9001', 'pay_relied_on', NULL,"
        " 'MISSING_IN_BANK', 488200, 488200, 'chase',"
        " 'The gateway settled this and the bank has no record of it.',"
        " 'Settled Rs 4,882.00, no matching credit.', 0)", (business_id,))
    led.conn.commit()
    led.close()

    class FakeAgent:
        def judge(self, forecast, business="", inputs=None, extra_tools=None):
            for tool in extra_tools:
                if tool.name == "recon_status":
                    tool.call({"payment_id": "pay_relied_on"})
            from agent.treasury_classifier import TreasuryVerdict

            return TreasuryVerdict(exception_code=forecast.finding,
                                   action=forecast.action, confidence=0.9,
                                   reasoning="ok")

    inputs, _ = generate(as_of=TODAY)
    result = run(inputs, agent=FakeAgent(), source="connected",
                 business_id=business_id)

    flags = result.verdict.recon_flagged
    assert len(flags) == 1
    assert flags[0]["payment_id"] == "pay_relied_on"
    assert flags[0]["run_id"] == "recon_run"
    assert flags[0]["finding"] == "MISSING_IN_BANK"
    assert flags[0]["at_stake"] == 488200

    payload = _json.loads(_json.dumps(result.as_dict()))
    assert payload["verdict"]["recon_flagged"][0]["run_id"] == "recon_run"


def test_no_recon_tool_call_leaves_recon_flagged_empty():
    class FakeAgent:
        def judge(self, forecast, business="", inputs=None, extra_tools=None):
            from agent.treasury_classifier import TreasuryVerdict

            return TreasuryVerdict(exception_code=forecast.finding,
                                   action=forecast.action, confidence=0.9,
                                   reasoning="ok")

    inputs, _ = generate(as_of=TODAY)
    result = run(inputs, agent=FakeAgent(), source="demo")
    assert result.verdict.recon_flagged == []
