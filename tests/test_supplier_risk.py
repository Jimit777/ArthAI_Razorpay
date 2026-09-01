"""
Tests for the supplier risk engine.

The thing being protected throughout: every number on that page is computed,
and the agent has nowhere to put one. The specification for this feature asked
the model to calculate the late-filing percentage and return a risk probability
as a float; both are arithmetic, and a model that is occasionally, silently
wrong about a figure destroys the only thing this product sells.
"""

import json
import sys
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.risk_agent import (RiskJudgment, _readable, review,  # noqa: E402
                              strict_schema, unverified_figures)
from engine.gst.filing_history import (DEFAULT_MONTHS, GSTR1_DUE_DAY,  # noqa: E402
                                       GSTR3B_DUE_DAY, Persona,
                                       SimulatedHistoryProvider,
                                       SupplierHistoryService,
                                       UploadedHistoryProvider, due_dates,
                                       history_for, normalise_period)
from engine.gst.risk import (ACT_WATCH, MIN_PERIODS, PATTERN_CLEAN,  # noqa: E402
                             PATTERN_DEFAULTER, PATTERN_LATE, PATTERN_THIN,
                             exposure_at_risk, profile, recommended_action)
from merchant.gstin_lookup import FilingStatusApi  # noqa: E402
from merchant.purchase_import import (SAMPLE_REGISTER,  # noqa: E402
                                      filing_history_csv, parse,
                                      parse_filing_history,
                                      sample_filing_history)
from merchant.risk_pipeline import run  # noqa: E402

PASSWORD = "a-good-password"


# --- the simulator --------------------------------------------------------

def test_a_history_is_the_same_every_time_it_is_read():
    """A risk profile that changed on refresh would be worse than none."""
    first = history_for("27AAAAA0000A1Z5")
    second = history_for("27AAAAA0000A1Z5")
    assert first.persona == second.persona
    assert first.as_rows() == second.as_rows()


def test_thirty_six_months_by_default():
    assert len(history_for("27AAAAA0000A1Z5").months) == DEFAULT_MONTHS


def test_due_dates_are_the_statutory_ones():
    for month in history_for("27AAAAA0000A1Z5").months:
        assert month.gstr1_due.day == GSTR1_DUE_DAY
        assert month.gstr3b_due.day == GSTR3B_DUE_DAY


def test_the_honest_enterprise_never_misses():
    prof = profile(history_for("27X", persona=Persona.HONEST))
    assert prof.compliance_bps == 10_000
    assert prof.sold_but_did_not_pay == 0
    assert prof.avg_gstr3b_delay_days == 0
    assert prof.trust_score == 100
    assert prof.pattern == PATTERN_CLEAN


def test_the_habitual_late_filer_always_pays_eventually():
    prof = profile(history_for("27X", persona=Persona.LATE))
    assert prof.compliance_bps == 10_000
    assert prof.sold_but_did_not_pay == 0
    assert prof.avg_gstr3b_delay_days >= 90
    assert prof.pattern == PATTERN_LATE


def test_the_defaulter_reports_sales_and_does_not_pay():
    """
    The dangerous one, and the reason this feature exists. A reconciliation
    cannot see it: they file GSTR-1, so the invoice appears in GSTR-2B and
    everything matches, while the tax was never paid.
    """
    prof = profile(history_for("27X", persona=Persona.DEFAULTER))
    assert prof.gstr1_filed > prof.gstr3b_filed
    assert prof.sold_but_did_not_pay > 0
    assert prof.pattern == PATTERN_DEFAULTER
    assert prof.trust_score < 60


def test_most_suppliers_are_fine():
    """A register where everyone is a risk teaches a merchant to ignore it."""
    import random

    from engine.gst.filing_history import _persona_for

    seen = [_persona_for(random.Random(f"filing:{i}")) for i in range(1000)]
    honest = sum(1 for p in seen if p is Persona.HONEST)
    assert 0.5 < honest / len(seen) < 0.75


# --- the arithmetic -------------------------------------------------------

def test_compliance_counts_only_months_they_reported_a_sale():
    """
    A month with no sales has no tax to pay. Counting it as a default would
    punish a supplier for being quiet.
    """
    prof = profile(history_for("27X", persona=Persona.HONEST))
    assert prof.compliance_bps == 10_000


def test_total_silence_cannot_look_like_perfect_compliance():
    """
    Regression. Compliance's denominator is GSTR-1 filings, so a supplier who
    files NOTHING is absent from both sides of the ratio. An erratic supplier
    silent for eight of thirty-six months scored 99/100 before coverage was
    added to the score.
    """
    prof = profile(history_for("27X", persona=Persona.ERRATIC))
    if prof.silent_periods:
        assert prof.coverage_bps < 10_000
        assert prof.trust_score < 100


def test_a_dead_registration_caps_the_score_whatever_the_history():
    from engine.gst.risk import DEAD_REGISTRATION_CAP, RiskProfile

    prof = RiskProfile(gstin="X", periods=36, gstr1_filed=36, gstr3b_filed=36,
                       gstr3b_known_periods=36,
                       registration_status="cancelled")
    assert prof.trust_score <= DEAD_REGISTRATION_CAP


def test_thin_history_is_not_scored_as_risk():
    from engine.gst.risk import RiskProfile

    prof = RiskProfile(gstin="X", periods=2, gstr1_filed=2, gstr3b_filed=0,
                       gstr3b_known_periods=2)
    assert not prof.enough_history
    assert prof.pattern == PATTERN_THIN
    assert prof.trust_score == 50, "unknown must not read as dangerous"


def test_exposure_at_risk_is_observed_frequency_not_a_forecast():
    """
    Their exposure multiplied by how often they have reported a sale and not
    paid the tax. A fact, not a prediction - and integer paise throughout.
    """
    prof = profile(history_for("27X", persona=Persona.DEFAULTER))
    at_risk = exposure_at_risk(100_000_00, prof)
    assert isinstance(at_risk, int)
    assert 0 < at_risk <= 100_000_00


def test_a_clean_supplier_puts_nothing_at_risk():
    prof = profile(history_for("27X", persona=Persona.HONEST))
    assert exposure_at_risk(100_000_00, prof) == 0


# --- the file ------------------------------------------------------------

def test_headers_do_not_have_to_match_anything():
    """
    A purchase register comes out of Tally, Zoho, Busy or a spreadsheet
    somebody made in 2019. Demanding an exact format means the first thing a
    merchant sees is a rejection.
    """
    data = (b"Party Name,GSTIN of Supplier,Bill No,Bill Date,Assessable Value,"
            b"Central Tax,State Tax,Integrated Tax\n"
            b"Anand,27GQRIR1135W5ZQ,A/1,2026-08-01,1000,90,90,0\n")
    result = parse(data, "r.csv")
    assert result.ok
    assert result.groups[0].current_month_total_tax_exposure == 18_000


def test_indian_number_formatting_is_read_correctly():
    data = (b'Supplier,GSTIN,CGST,SGST,IGST\n'
            b'Anand,27GQRIR1135W5ZQ,"1,20,000.50",0,0\n')
    assert parse(data, "r.csv").total_tax == 12_000_050


def test_a_row_without_a_gstin_is_named_not_swallowed():
    """There is nothing to join it to a filing history on."""
    data = (b"Supplier,GSTIN,CGST,SGST,IGST\n"
            b"Ghost,NOTAGSTIN,10,10,0\n"
            b"Anand,27GQRIR1135W5ZQ,90,90,0\n")
    result = parse(data, "r.csv")
    assert len(result.groups) == 1
    assert result.rows_skipped and "NOTAGSTIN" in result.rows_skipped[0]


def test_invoices_from_one_supplier_are_summed():
    data = (b"Supplier,GSTIN,CGST,SGST,IGST\n"
            b"Anand,27GQRIR1135W5ZQ,90,90,0\n"
            b"Anand,27GQRIR1135W5ZQ,10,10,0\n")
    result = parse(data, "r.csv")
    assert len(result.groups) == 1
    assert result.groups[0].invoice_count == 2
    assert result.groups[0].current_month_total_tax_exposure == 20_000


def test_money_crosses_the_file_boundary_once_and_becomes_integers():
    result = parse(SAMPLE_REGISTER.encode(), "sample.csv")
    for group in result.groups:
        assert isinstance(group.current_month_total_tax_exposure, int)
        for invoice in group.invoices:
            for value in (invoice.cgst, invoice.sgst, invoice.igst,
                          invoice.taxable_value):
                assert isinstance(value, int)


def test_the_sample_register_contains_one_of_each_kind():
    """A demo that draws suppliers at random has a one-in-six chance of
    containing nothing worth looking at."""
    portfolio = run(parse(SAMPLE_REGISTER.encode(), "s.csv"), use_agent=False)
    patterns = {s.pattern for s in portfolio.suppliers}
    assert PATTERN_DEFAULTER in patterns
    assert PATTERN_CLEAN in patterns
    assert PATTERN_LATE in patterns


# --- the agent has nowhere to put a number -------------------------------

def test_the_schema_cannot_carry_a_figure():
    fields = set(strict_schema()["properties"])
    for banned in ("risk", "probability", "score", "rate", "percent",
                   "amount", "days"):
        assert not any(banned in f for f in fields), fields


def test_the_pattern_field_survives_the_schema_stripper():
    """
    Regression. The stripper filtered by key name at every depth, so a FIELD
    called `pattern` was deleted because `pattern` is also a JSON Schema
    keyword - and the model was asked for an object whose most important
    property did not exist.
    """
    assert "pattern" in strict_schema()["properties"]


def test_a_whole_rupee_figure_is_not_called_invented():
    """
    "Rs 27,000" for a supplied "Rs 27,000.00" is the same number said normally.
    Flagging it cost the model confidence on a correct answer.
    """
    assert unverified_figures("pay the Rs 27,000 now", "credit Rs 27,000.00") == []


def test_a_genuinely_invented_figure_is_still_caught():
    assert unverified_figures("you will lose Rs 9,99,999.00", "Rs 27,000.00")


def test_a_stray_escape_sequence_is_repaired():
    assert "—" in _readable("pay the tax \\u2014 expect a lag")


def test_a_dead_registration_can_never_be_called_safe_to_pay():
    from engine.gst.risk import RiskProfile

    prof = RiskProfile(gstin="X", periods=36, gstr1_filed=36, gstr3b_filed=36,
                       gstr3b_known_periods=36,
                       registration_status="suspended")
    judged = RiskJudgment(pattern=PATTERN_CLEAN, action="safe_to_pay",
                          headline="fine", reasoning="fine")
    verdict = review(prof, "Ghost Ltd", judged, evidence="")
    assert verdict.action == "hold_payment"
    assert verdict.corrections


def test_disagreeing_with_the_arithmetic_is_recorded():
    """The agent may see a trend the pattern rules cannot. It does not get to
    do so silently."""
    prof = profile(history_for("27X", persona=Persona.DEFAULTER))
    judged = RiskJudgment(pattern=PATTERN_CLEAN, action="safe_to_pay",
                          headline="fine", reasoning="fine")
    verdict = review(prof, "Deepak", judged, evidence="")
    assert any("reads as" in c for c in verdict.corrections)


# --- the pipeline ---------------------------------------------------------

def test_the_numbers_survive_the_agent_being_switched_off():
    """
    Everything countable is computed before the model is asked, so a failed
    call leaves a usable table rather than an empty one.
    """
    portfolio = run(parse(SAMPLE_REGISTER.encode(), "s.csv"), use_agent=False)
    assert portfolio.suppliers
    for supplier in portfolio.suppliers:
        assert supplier.trust_score
        assert supplier.pattern
        assert supplier.exposure > 0


def test_the_portfolio_totals_are_the_sum_of_the_rows():
    portfolio = run(parse(SAMPLE_REGISTER.encode(), "s.csv"), use_agent=False)
    assert portfolio.total_exposure == sum(
        s.exposure for s in portfolio.suppliers)
    assert portfolio.total_at_risk == sum(
        s.at_risk for s in portfolio.suppliers)
    assert portfolio.total_at_risk <= portfolio.total_exposure


def test_the_payload_keeps_the_invoices_under_each_supplier():
    """A merchant who disagrees with a row needs to see what it was built
    from."""
    payload = run(parse(SAMPLE_REGISTER.encode(), "s.csv"),
                  use_agent=False).as_dict()
    assert payload["portfolio"]["suppliers"] == len(payload["suppliers"])
    for supplier in payload["suppliers"]:
        assert supplier["invoices"]
        assert sum(i["total_tax"] for i in supplier["invoices"]) \
            == supplier["exposure"]


def test_suppliers_are_judged_one_at_a_time_never_pooled():
    """
    A hundred suppliers in one prompt is a hundred chances to blend two of
    them, and that failure is a fluent paragraph about the wrong company.
    """
    seen = []

    class Spy:
        def judge_all(self, jobs, on_each=None):
            from agent.risk_agent import RiskVerdict

            seen.append(len(jobs))
            return [RiskVerdict(gstin=j[0].gstin, supplier_name=j[1],
                                pattern=j[0].pattern, action="safe_to_pay",
                                headline="h", reasoning="r") for j in jobs]

    imported = parse(SAMPLE_REGISTER.encode(), "s.csv")
    portfolio = run(imported, use_agent=True, agent=Spy())
    assert seen == [len(imported.groups)], "jobs must be one per supplier"
    assert len(portfolio.suppliers) == len(imported.groups)


# --- the page -------------------------------------------------------------

@pytest.fixture
def shop(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    client.post("/sources/simulator")
    return client


def _analyse(client, data=None, use_agent="no", timeout=30):
    """
    Upload a register and wait for the run.

    Ensures supplier history exists first, because a register with nothing to
    be scored against is now refused rather than quietly scored off generated
    records. That refusal is the point of the three-tab split, so the helper
    goes through the real Without API flow instead of around it.
    """
    import merchant.app as appmod

    client.post("/agents/input-credit/history",
                files={"history": ("h.csv", sample_filing_history().encode(),
                                   "text/csv")})
    r = client.post(
        "/agents/input-credit",
        files={"register": ("r.csv", data or SAMPLE_REGISTER.encode(),
                            "text/csv")},
        data={"use_agent": use_agent}, follow_redirects=False)
    key = r.headers["location"].split("key=")[-1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        with appmod._risk_lock:
            state = dict(appmod.RISK_RUNS.get(key) or {})
        if state.get("state") != "running":
            return key, state
        time.sleep(0.05)
    raise AssertionError("the analysis never finished")


def test_the_tab_exists_inside_the_input_credit_workspace(shop):
    page = shop.get("/agents/input-credit").text
    assert "Supplier risk" in page
    assert shop.get("/agents/input-credit").status_code == 200


def test_uploading_a_register_produces_a_portfolio(shop):
    shop.post("/agents/input-credit/history",
              files={"history": ("h.csv", sample_filing_history().encode(),
                                 "text/csv")})
    key, state = _analyse(shop)
    assert state["state"] == "done"
    page = shop.get(f"/agents/input-credit?key={key}").text
    assert "input credit pending" in page
    assert "their record puts at risk" in page
    assert "Deepak Packaging" in page


def test_the_json_payload_is_available_on_its_own(shop):
    key, _state = _analyse(shop)
    payload = shop.get(f"/agents/input-credit/{key}.json").json()
    assert payload["portfolio"]["suppliers"] == len(payload["suppliers"])
    assert payload["suppliers"][0]["invoices"]


def test_a_file_with_no_usable_rows_says_why(shop):
    _key, state = _analyse(shop, data=b"nothing,useful\n1,2\n")
    assert state["state"] == "failed"
    assert "GSTIN" in state["phase"] or "columns" in state["phase"]


def test_a_sample_register_can_be_downloaded(shop):
    response = shop.get("/agents/input-credit/sample")
    assert response.status_code == 200
    assert "GSTIN" in response.text
    assert "attachment" in response.headers["content-disposition"]


def test_the_page_says_the_history_is_simulated(shop):
    """
    In demo mode the page must say the filing dates are made up.

    Not a cosmetic assertion. Everything below that badge - a trust score, a
    recommendation to stop buying from a named company - reads as fact, and in
    demo mode none of it is. The warning is the feature.
    """
    page = shop.get("/agents/input-credit").text
    assert "Demo mode" in page
    assert "Do not act on any of it against a real supplier" in page


def test_the_demo_warning_survives_onto_the_results(shop):
    """A warning only on the upload screen is a warning nobody reads."""
    key = shop.post("/agents/input-credit/demo", data={"use_agent": "no"},
                    follow_redirects=False
                    ).headers["location"].split("key=")[-1]
    _finish(shop, key)
    page = shop.get(f"/agents/input-credit?key={key}").text
    assert "simulated filing history" in page
    assert "Do not act on these against a real supplier" in page


def test_the_disclaimer_is_a_banner(shop):
    key, _state = _analyse(shop)
    page = shop.get(f"/agents/input-credit?key={key}").text
    assert 'class="banner brand"' in page
    assert "filed, claimed or paid" in page


# --- what a run cost -----------------------------------------------------

def test_the_pipeline_keeps_the_token_cost():
    """
    The verdicts carried it and the pipeline threw it away, so the page could
    not say what a click had spent - the one number a person paying for the
    API actually wants.
    """
    from agent.risk_agent import RiskVerdict

    class Spy:
        def judge_all(self, jobs, on_each=None):
            return [RiskVerdict(gstin=j[0].gstin, supplier_name=j[1],
                                pattern=j[0].pattern, action="safe_to_pay",
                                headline="h", reasoning="r",
                                input_tokens=10, output_tokens=800,
                                cache_read_tokens=4_000) for j in jobs]

    portfolio = run(parse(SAMPLE_REGISTER.encode(), "s.csv"),
                    use_agent=True, agent=Spy())
    assert portfolio.usage.calls == len(portfolio.suppliers)
    assert portfolio.usage.output_tokens == 800 * len(portfolio.suppliers)
    assert portfolio.usage.usd > 0


def test_the_cost_appears_in_the_payload():
    from agent.risk_agent import RiskVerdict

    class Spy:
        def judge_all(self, jobs, on_each=None):
            return [RiskVerdict(gstin=j[0].gstin, supplier_name=j[1],
                                pattern=j[0].pattern, action="safe_to_pay",
                                headline="h", reasoning="r",
                                input_tokens=10, output_tokens=800,
                                cache_read_tokens=4_000) for j in jobs]

    payload = run(parse(SAMPLE_REGISTER.encode(), "s.csv"),
                  use_agent=True, agent=Spy()).as_dict()
    usage = payload["portfolio"]["usage"]
    assert usage["calls"] and usage["usd"] > 0
    assert 0 <= usage["cached_share_pct"] <= 100


def test_a_run_without_the_agent_costs_nothing():
    portfolio = run(parse(SAMPLE_REGISTER.encode(), "s.csv"), use_agent=False)
    assert portfolio.usage.calls == 0
    assert portfolio.usage.usd == 0


def test_cached_input_is_priced_a_tenth_of_fresh_input():
    """The whole reason the prompts are arranged so the expensive part repeats."""
    from agent.pricing import Usage

    fresh = Usage(input_tokens=10_000, calls=1)
    cached = Usage(cache_read_tokens=10_000, calls=1)
    assert abs(fresh.usd - cached.usd * 10) < 1e-9


def test_the_price_list_lives_in_one_place():
    """It was inline in audit.py and nowhere else. Three copies of a price
    list is three things to forget when a price changes."""
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent
    for path in list(root.glob("*.py")) + list(root.glob("agent/*.py")) \
            + list(root.glob("merchant/*.py")):
        if path.name == "pricing.py":
            continue
        source = path.read_text()
        assert not re.search(r"\* 25\)\s*[*/]", source), \
            f"{path.name} looks like it prices tokens itself"


# --- the recommendation does not change between runs ---------------------

def test_the_same_register_always_recommends_the_same_thing():
    """
    Regression. The action used to be whatever the model returned, and on a
    borderline record it changed between runs - the same supplier came back
    "pay, but keep watching" one afternoon and "safe to pay" the next, with
    sound reasoning both times. A merchant who refreshes and gets different
    advice on unchanged data stops believing the advice.
    """
    seen = set()
    for _ in range(5):
        portfolio = run(parse(SAMPLE_REGISTER.encode(), "s.csv"),
                        use_agent=False)
        seen.add(tuple((s.gstin, s.action) for s in portfolio.suppliers))
    assert len(seen) == 1


def test_a_dead_registration_outranks_everything_in_the_ladder():
    """
    Credit claimed against one comes back with interest whatever the filing
    history says - so neither state can ever come out as safe to pay. Which of
    the two it lands on is the sharper distinction tested below.
    """
    from engine.gst.risk import ACT_SAFE, RiskProfile, recommended_action

    for status in ("cancelled", "suspended"):
        spotless_but_dead = RiskProfile(
            gstin="X", periods=36, gstr1_filed=36, gstr3b_filed=36,
            gstr3b_known_periods=36, registration_status=status)
        assert recommended_action(spotless_but_dead) != ACT_SAFE, status


def test_thin_history_recommends_caution_not_confidence():
    from engine.gst.risk import ACT_WATCH, RiskProfile, recommended_action

    thin = RiskProfile(gstin="X", periods=2, gstr1_filed=2, gstr3b_filed=2,
                       gstr3b_known_periods=2)
    assert recommended_action(thin) == ACT_WATCH


def test_one_missed_payment_is_enough_to_stop_saying_safe():
    from engine.gst.risk import ACT_SAFE, RiskProfile, recommended_action

    almost = RiskProfile(gstin="X", periods=36, gstr1_filed=36,
                         gstr3b_filed=35, gstr3b_known_periods=36,
                         sold_but_did_not_pay=1)
    assert recommended_action(almost) != ACT_SAFE


def test_the_agent_may_not_relax_the_recommendation():
    """
    It can still read a trend the ladder cannot, and saying so is useful. What
    it cannot do is talk the merchant into paying a supplier the figures say to
    hold.
    """
    from agent.risk_agent import RiskJudgment, review
    from engine.gst.filing_history import Persona, history_for
    from engine.gst.risk import (ACT_HOLD, ACT_STOP, PATTERN_DEFAULTER,
                                 recommended_action)
    from engine.gst.risk import profile as profile_history

    defaulter = profile_history(history_for("27X", persona=Persona.DEFAULTER))
    relaxed = RiskJudgment(pattern=PATTERN_DEFAULTER, action="safe_to_pay",
                           headline="looks fine", reasoning="they seem ok")
    verdict = review(defaulter, "Deepak", relaxed, evidence="")

    # Whichever rung the record lands on - the point is that the agent's
    # softer answer is not the one that reaches the merchant.
    assert verdict.action == recommended_action(defaulter)
    assert verdict.action in (ACT_HOLD, ACT_STOP)
    assert verdict.agent_action == "safe_to_pay"
    assert not verdict.goes_further
    assert any("would have said" in c for c in verdict.corrections)


def test_the_agent_wanting_to_go_further_is_recorded_not_discarded():
    from agent.risk_agent import RiskJudgment, review
    from engine.gst.risk import (ACT_SAFE, PATTERN_CLEAN, RiskProfile)

    spotless = RiskProfile(gstin="X", periods=36, gstr1_filed=36,
                           gstr3b_filed=36, gstr3b_known_periods=36)
    stricter = RiskJudgment(pattern=PATTERN_CLEAN, action="hold_payment",
                            headline="something is off",
                            reasoning="the last three months look wrong")
    verdict = review(spotless, "Anand", stricter, evidence="")

    assert verdict.action == ACT_SAFE, "the figures still decide"
    assert verdict.goes_further is True
    assert verdict.agent_action == "hold_payment"


def test_a_failed_call_falls_back_to_the_same_ladder():
    from agent.risk_agent import ClaudeRiskAgent
    from engine.gst.filing_history import Persona, history_for
    from engine.gst.risk import profile as profile_history, recommended_action

    prof = profile_history(history_for("27X", persona=Persona.DEFAULTER))
    agent = ClaudeRiskAgent.__new__(ClaudeRiskAgent)
    agent._model = "claude-opus-5"
    verdict = agent._failed(prof, "Deepak", "no route to host", 0.0)
    assert verdict.action == recommended_action(prof)


# --- stopping, as distinct from holding -----------------------------------

def test_a_cancelled_registration_means_stop_not_hold():
    """
    A suspension can be revoked and the credit recovered. A cancellation means
    every future invoice from them is unclaimable, and no amount of chasing
    changes that - so they are different decisions.
    """
    from engine.gst.risk import (ACT_HOLD, ACT_STOP, RiskProfile,
                                 recommended_action)

    spotless = dict(gstin="X", periods=36, gstr1_filed=36, gstr3b_filed=36,
                    gstr3b_known_periods=36)
    assert recommended_action(
        RiskProfile(**spotless, registration_status="cancelled")) == ACT_STOP
    assert recommended_action(
        RiskProfile(**spotless, registration_status="suspended")) == ACT_HOLD


def test_a_settled_pattern_of_default_means_stop():
    from engine.gst.risk import ACT_STOP, RiskProfile, recommended_action

    terminal = RiskProfile(
        gstin="X", periods=36, gstr1_filed=36, gstr3b_filed=8,
        gstr3b_known_periods=36,
        sold_but_did_not_pay=28, recent_periods=12,
        recent_sold_but_did_not_pay=10)
    assert recommended_action(terminal) == ACT_STOP


def test_a_bad_run_on_thin_recent_history_is_not_enough_to_stop():
    """
    Stopping is a decision about the NEXT order, so it needs evidence the
    pattern is settled rather than a bad quarter.
    """
    from engine.gst.risk import ACT_STOP, RiskProfile, recommended_action

    thin = RiskProfile(gstin="X", periods=6, gstr1_filed=6, gstr3b_filed=1,
                       sold_but_did_not_pay=5, recent_periods=4,
                       recent_sold_but_did_not_pay=4)
    assert recommended_action(thin) != ACT_STOP


def test_bad_but_not_terminal_still_means_hold():
    """
    Holding is leverage over invoices already raised and is recoverable the
    moment they file. It is the more useful answer while there is still a
    chance they will.
    """
    from engine.gst.risk import ACT_HOLD, RiskProfile, recommended_action

    bad = RiskProfile(gstin="X", periods=36, gstr1_filed=36, gstr3b_filed=11,
                      gstr3b_known_periods=36,
                      sold_but_did_not_pay=25, recent_periods=12,
                      recent_sold_but_did_not_pay=7)
    assert recommended_action(bad) == ACT_HOLD


def test_the_notice_says_what_was_actually_decided():
    """
    A letter offering to release a payment, sent to a supplier you have
    stopped buying from, offers leverage that is no longer on the table.
    """
    from agent.vendor_documents import vendor_notice

    supplier = {
        "supplier_name": "Ghost Traders", "gstin": "29ABCDE1234F1Z5",
        "at_risk": 50_00_000,
        "invoices": [{"invoice_number": "G/1", "invoice_date": "2026-08-02",
                      "total_tax": 50_00_000}],
        "profile": {"gstr1_filed": 36, "gstr3b_filed": 2,
                    "sold_but_did_not_pay": 34,
                    "registration_status": "cancelled"}}

    holding = vendor_notice({**supplier, "action": "hold_payment"}).body
    stopping = vendor_notice({**supplier, "action": "stop_buying"}).body

    assert "continue our" in holding and "suspended further orders" not in holding
    assert "suspended further orders" in stopping
    assert "Suspension of further orders" in stopping
    assert "willing to review this decision" in stopping


def test_every_rung_of_the_ladder_has_a_label():
    """A recommendation the interface cannot name is one nobody can act on."""
    from agent.risk_agent import ACTION_LABEL
    from engine.gst.risk import (ACT_HOLD, ACT_SAFE, ACT_STOP, ACT_WATCH,
                                 ACTION_SEVERITY)

    for action in (ACT_SAFE, ACT_WATCH, ACT_HOLD, ACT_STOP):
        assert action in ACTION_LABEL, action
        assert action in ACTION_SEVERITY, action


# --- one contract, three sources -----------------------------------------
#
# The claim this refactor makes is that where filing history came from changes
# nothing except a provenance label. These tests are what makes that a claim
# rather than an aspiration - and they are written to fail loudly if anyone
# later adds a shortcut for one mode that the others do not get.

def _api_serving(histories):
    """
    A fake filing-status API that serves given histories in the GSTN's shape.

    Deliberately the government's own field names and its MMYYYY period
    ordering, so the parser is exercised against the format it will actually
    meet rather than a convenient one.
    """
    class Response:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def http(_method, url, **_kw):
        gstin = url.rsplit("/", 1)[-1]
        entries = []
        for month in histories[gstin].months:
            year, mm = month.period.split("-")
            gstn_period = f"{mm}{year}"
            entries.append(
                {"rtntype": "GSTR1", "ret_prd": gstn_period,
                 "dof": month.gstr1_filed.strftime("%d-%m-%Y")}
                if month.gstr1_filed else
                {"rtntype": "GSTR1", "ret_prd": gstn_period,
                 "status": "Not Filed"})
            entries.append(
                {"rtntype": "GSTR3B", "ret_prd": gstn_period,
                 "dof": month.gstr3b_filed.strftime("%d-%m-%Y")}
                if month.gstr3b_filed else
                {"rtntype": "GSTR3B", "ret_prd": gstn_period,
                 "status": "Not Filed"})
        return Response({"EFiledlist": entries})

    return FilingStatusApi(url_template="https://example.test/returns/{gstin}",
                           http=http)


def _three_services():
    """The same eight suppliers' history, reached three different ways."""
    imported = parse(SAMPLE_REGISTER.encode(), "register.csv")
    gstins = [g.supplier_gstin for g in imported.groups]
    simulated = {g: history_for(g) for g in gstins}

    uploaded = parse_filing_history(
        filing_history_csv(simulated.values()).encode(), "history.csv")

    # Registration status is joined on for every source, because it is a
    # separate lookup in reality - the portal answers "what did they file" and
    # "is this registration alive" with two different calls, and a returns
    # feed carries only the first. A Mode A integration makes both; so does
    # history_service_for; so does this.
    statuses = {g: h.registration_status for g, h in simulated.items()}

    return imported, {
        "api": SupplierHistoryService(_api_serving(simulated),
                                      statuses=statuses),
        "file": SupplierHistoryService(
            UploadedHistoryProvider(uploaded.histories), statuses=statuses),
        "simulated": SupplierHistoryService(SimulatedHistoryProvider(),
                                            statuses=statuses),
    }


def _without_provenance(payload):
    """Everything except the fields that are SUPPOSED to differ by mode."""
    payload = json.loads(json.dumps(payload))
    for field in ("history_source", "history_source_label",
                  "history_source_note", "history_is_demo",
                  "history_failures"):
        payload["portfolio"].pop(field, None)
    for supplier in payload["suppliers"]:
        supplier.pop("history_source", None)
    return payload


def test_all_three_sources_produce_the_same_payload():
    """
    The whole point of the abstraction, asserted on the whole payload.

    Not a spot check on a couple of fields: the entire nested structure is
    compared, so a figure that starts varying by source fails here even if
    nobody thought to write a test for that particular figure.
    """
    imported, services = _three_services()
    payloads = {name: run(imported, use_agent=False, history=service).as_dict()
                for name, service in services.items()}

    stripped = {name: _without_provenance(p) for name, p in payloads.items()}
    assert stripped["api"] == stripped["file"]
    assert stripped["file"] == stripped["simulated"]


def test_every_source_reports_the_same_schema():
    """Same keys, whatever produced the run - including the provenance keys."""
    imported, services = _three_services()
    shapes = []
    for service in services.values():
        payload = run(imported, use_agent=False, history=service).as_dict()
        shapes.append((sorted(payload["portfolio"]),
                       sorted(payload["suppliers"][0])))
    assert shapes[0] == shapes[1] == shapes[2]


def test_each_source_names_itself_in_the_payload():
    """A figure a merchant cannot trace to its source is a figure they cannot
    calibrate their trust against."""
    imported, services = _three_services()
    for name, service in services.items():
        portfolio = run(imported, use_agent=False, history=service).as_dict()
        assert portfolio["portfolio"]["history_source"] == name
        assert all(s["history_source"] == name
                   for s in portfolio["suppliers"])

    demo = run(imported, use_agent=False,
               history=services["simulated"]).as_dict()
    assert demo["portfolio"]["history_is_demo"] is True
    for real in ("api", "file"):
        payload = run(imported, use_agent=False,
                      history=services[real]).as_dict()
        assert payload["portfolio"]["history_is_demo"] is False


def test_the_grid_and_the_clocks_are_built_the_same_way_in_every_mode():
    """The drawer's two interactive pieces must not be a simulator privilege."""
    imported, services = _three_services()
    for service in services.values():
        payload = run(imported, use_agent=False, history=service).as_dict()
        for supplier in payload["suppliers"]:
            # Length follows the supplier's own record - see the grid test in
            # test_supplier_drawer for why it is not a fixed 36.
            assert supplier["compliance_grid"]
            assert len(supplier["compliance_grid"]) <= DEFAULT_MONTHS
            assert supplier["clocks"]["invoices"]
            assert supplier["clocks"]["window_days"] == 180


# --- absence is not innocence --------------------------------------------

def test_a_supplier_missing_from_an_upload_is_unknown_not_clean():
    """
    The single most important rule in the ingestion layer.

    Silently simulating a supplier the file does not mention would put a
    confident, invented number under a real company's name. Empty scores as
    TOO_LITTLE_HISTORY, which is the truthful reading of "we have no idea".
    """
    provider = UploadedHistoryProvider({})
    history = provider.history_for("27AAAAA0000A1Z5")

    assert history.months == []
    assert history.known is False
    assert profile(history).pattern == PATTERN_THIN
    assert recommended_action(profile(history)) == ACT_WATCH


def test_a_missing_supplier_is_counted_and_reported():
    """A table of quiet 'unknown' rows that look like a verdict is worse than
    a banner saying how many could not be found."""
    imported = parse(SAMPLE_REGISTER.encode(), "register.csv")
    only_one = imported.groups[0].supplier_gstin
    provider = UploadedHistoryProvider(
        {only_one: history_for(only_one)})

    payload = run(imported, use_agent=False,
                  history=SupplierHistoryService(provider)).as_dict()

    assert payload["portfolio"]["suppliers_without_history"] == \
        len(imported.groups) - 1
    known = [s for s in payload["suppliers"] if s["history_known"]]
    assert [s["gstin"] for s in known] == [only_one]


def test_a_blank_filing_date_is_an_assertion_not_a_gap():
    """
    The distinction the whole file format turns on.

    A row with an empty GSTR-3B cell says someone checked and it was not filed;
    that is a countable default. A period with no row at all says nothing and
    must not be counted against anyone.
    """
    asserted = parse_filing_history(
        b"GSTIN,Period,GSTR-1 Filed Date,GSTR-3B Filed Date\n"
        b"27AAAAA0000A1Z5,2026-07,2026-08-10,\n"
        b"27AAAAA0000A1Z5,2026-08,2026-09-10,\n",
        "history.csv")
    history = asserted.histories["27AAAAA0000A1Z5"]

    assert len(history.months) == 2
    assert all(m.sold_but_did_not_pay for m in history.months)
    assert profile(history).sold_but_did_not_pay == 2

    # The periods nobody supplied a row for simply are not there.
    assert profile(history).periods == 2


def test_a_file_without_a_gstr3b_column_is_refused():
    """
    Refusing beats guessing, and here the guess is catastrophic.

    Reading a GSTR-1-only file would set every supplier's GSTR-3B count to
    zero - 'reported sales, never paid the tax', the most serious finding this
    product makes - about a merchant's entire supplier book.
    """
    result = parse_filing_history(
        b"GSTIN,Period,GSTR-1 Filed Date\n"
        b"27AAAAA0000A1Z5,2026-07,2026-08-10\n", "history.csv")

    assert not result.ok
    assert "gstr3b_filed" in result.missing_columns


# --- reading what providers actually send --------------------------------

def test_the_gstn_period_ordering_is_read_correctly():
    """MMYYYY from the portal, YYYYMM from a spreadsheet. Getting this wrong
    moves a filing into another month and changes whether it was late."""
    assert normalise_period("072026") == "2026-07"
    assert normalise_period("2026-07") == "2026-07"
    assert normalise_period("07-2026") == "2026-07"
    assert normalise_period("2026-07-15") == "2026-07"
    assert normalise_period("garbage") is None
    assert normalise_period("2026-13") is None


def test_due_dates_land_in_the_following_month():
    """Both returns are due the month AFTER the period, which is what a naive
    implementation gets wrong every December."""
    assert due_dates("2026-07") == (date(2026, 8, GSTR1_DUE_DAY),
                                    date(2026, 8, GSTR3B_DUE_DAY))
    assert due_dates("2026-12") == (date(2027, 1, GSTR1_DUE_DAY),
                                    date(2027, 1, GSTR3B_DUE_DAY))


def test_an_unrecognised_status_word_is_read_as_not_filed():
    """The cautious direction. Rounding an unknown status to 'filed' turns a
    defaulter into a clean supplier, which is the error that costs money."""
    history = FilingStatusApi.to_history("27AAAAA0000A1Z5", {"EFiledlist": [
        {"rtntype": "GSTR1", "ret_prd": "072026", "dof": "10-08-2026"},
        {"rtntype": "GSTR3B", "ret_prd": "072026", "status": "Under Process"},
    ]})
    assert history.months[0].sold_but_did_not_pay is True


def test_the_earliest_filing_date_wins_over_a_revision():
    """A period can be filed then revised. The original date is what decides
    whether it was late, so an amendment must not make a punctual filer look
    late."""
    history = FilingStatusApi.to_history("27AAAAA0000A1Z5", {"EFiledlist": [
        {"rtntype": "GSTR3B", "ret_prd": "072026", "dof": "19-08-2026"},
        {"rtntype": "GSTR3B", "ret_prd": "072026", "dof": "28-11-2026"},
    ]})
    assert history.months[0].gstr3b_filed == date(2026, 8, 19)
    assert history.months[0].gstr3b_late_days == 0


def test_an_unreachable_api_does_not_fall_back_to_the_simulator():
    """
    The failure mode this whole abstraction exists to prevent.

    Half a table of genuine records and half of generated ones, with no column
    saying which, is worse than either alone - it is a screen nobody can
    calibrate. An unreachable lookup returns unknown and is reported.
    """
    def broken(*_a, **_kw):
        raise ConnectionError("provider is down")

    api = FilingStatusApi(url_template="https://example.test/{gstin}",
                          http=broken)
    history = api.history_for("27AAAAA0000A1Z5")

    assert history.months == []
    assert history.known is False
    assert api.failures and "provider is down" in api.failures[0][1]


def test_api_failures_reach_the_payload():
    """So the page can say how many could not be read."""
    def broken(*_a, **_kw):
        raise ConnectionError("timed out")

    imported = parse(SAMPLE_REGISTER.encode(), "register.csv")
    payload = run(imported, use_agent=False,
                  history=SupplierHistoryService(
                      FilingStatusApi(url_template="https://x.test/{gstin}",
                                      http=broken))).as_dict()

    assert len(payload["portfolio"]["history_failures"]) == len(imported.groups)
    assert payload["portfolio"]["suppliers_without_history"] == \
        len(imported.groups)


# --- the round trip a person can check by hand ---------------------------

def test_the_sample_history_reproduces_the_simulated_run():
    """
    Download the sample history, upload it, get the same answers.

    This is the demonstration that the mode does not change the answer, and it
    is a test rather than a README sentence so it cannot quietly stop being
    true.
    """
    imported = parse(SAMPLE_REGISTER.encode(), "register.csv")
    uploaded = parse_filing_history(sample_filing_history().encode(), "h.csv")

    simulated = run(imported, use_agent=False,
                    history=SupplierHistoryService(
                        SimulatedHistoryProvider())).as_dict()
    from_file = run(imported, use_agent=False,
                    history=SupplierHistoryService(
                        UploadedHistoryProvider(uploaded.histories))).as_dict()

    for a, b in zip(simulated["suppliers"], from_file["suppliers"]):
        assert a["gstin"] == b["gstin"]
        assert a["trust_score"] == b["trust_score"]
        assert a["pattern"] == b["pattern"]
        assert a["action"] == b["action"]
        assert a["at_risk"] == b["at_risk"]
        assert a["compliance_grid"] == b["compliance_grid"]


# --- mode A configuration -------------------------------------------------

@pytest.fixture
def biz(tmp_path, monkeypatch):
    """A signed-in owner with one business, for the configuration routes."""
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    return client


@pytest.mark.parametrize("url,why", [
    ("http://provider.test/returns/{gstin}", "must be https"),
    ("https://provider.test/returns/", "{gstin} placeholder"),
])
def test_a_bad_endpoint_is_refused_before_anything_is_stored(biz, url, why):
    """
    Both refusals matter. A URL with no placeholder would query the same
    supplier every time and quietly score a whole book against one company's
    record; an http URL puts a GST API key in cleartext on every hop.
    """
    response = biz.post("/agents/input-credit/filing-api",
                        data={"url_template": url, "api_key": "k",
                              "key_header": "x-api-key"},
                        follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

    # Still the connect form, because nothing was stored.
    page = biz.get("/agents/input-credit/with-api").text
    assert "Connect a GST filing-status API" in page


def test_a_key_with_nowhere_to_go_is_refused(biz):
    """A key with neither a header nor a parameter name would be silently
    dropped, and the merchant would think they were authenticated."""
    response = biz.post("/agents/input-credit/filing-api",
                        data={"url_template": "https://p.test/{gstin}",
                              "api_key": "secret"},
                        follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_a_configured_api_becomes_the_active_source(biz):
    """
    State 2: one upload box, because the register is the only thing the
    platform cannot fetch for itself.
    """
    biz.post("/agents/input-credit/filing-api",
             data={"url_template": "https://p.test/{gstin}"})

    page = biz.get("/agents/input-credit/with-api").text
    assert "Connected GST API" in page
    assert "Upload your purchase register" in page
    # No history box: it is fetched, not asked for.
    assert "Step 1" not in page
    assert "Import GSTR-2B" not in page

    biz.post("/agents/input-credit/filing-api/forget")
    assert "Connect a GST filing-status API" in \
        biz.get("/agents/input-credit/with-api").text


def test_staff_cannot_change_where_filing_history_comes_from(tmp_path,
                                                             monkeypatch):
    """
    Same reasoning as the rate card.

    Whoever decides where filing history comes from decides what every
    supplier score is computed against - point it at an endpoint that reports
    everyone as compliant and the findings quietly stop appearing.
    """
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    owner = TestClient(appmod.app)
    owner.post("/signup", data={"email": "owner@x.in", "password": PASSWORD})
    owner.post("/businesses", data={"name": "Meera's Boutique"})

    staff = TestClient(appmod.app)
    staff.post("/signup", data={"email": "staff@x.in", "password": PASSWORD})

    with appmod.ledger(None) as led:
        from merchant.auth import Auth, Role

        auth = Auth(led.conn)
        user = auth.by_email("staff@x.in")
        business = led.conn.execute(
            "SELECT business_id FROM businesses LIMIT 1").fetchone()
        auth.add_member(business["business_id"], user["user_id"], Role.STAFF)

    # Switching is a GET. Assert it worked rather than assuming - a staff
    # member who never entered the business would be refused for the wrong
    # reason and this test would pass without testing anything.
    staff.get(f"/switch?business_id={business['business_id']}")
    assert staff.get("/agents/input-credit").status_code == 200

    response = staff.post("/agents/input-credit/filing-api",
                          data={"url_template": "https://evil.test/{gstin}"},
                          follow_redirects=False)
    assert response.status_code == 403

    with appmod.ledger(None) as led:
        from merchant.sources import Sources

        assert Sources(led.conn).filing_api_config(
            business["business_id"]) is None

    # And the owner, in the same business, can.
    assert owner.post("/agents/input-credit/filing-api",
                      data={"url_template": "https://good.test/{gstin}"},
                      follow_redirects=False).status_code == 303


# --- three tabs, one dashboard -------------------------------------------
#
# The tabs name the one question that differs between them: where does supplier
# filing history come from? Everything downstream is deliberately identical, so
# these tests check both halves - that each tab asks for the right thing, and
# that what comes out the other end does not depend on which one you used.

DEMO = "/agents/input-credit"
WITHOUT_API = "/agents/input-credit/without-api"
WITH_API = "/agents/input-credit/with-api"


def _finish(client, key, timeout=30):
    import merchant.app as appmod

    deadline = time.time() + timeout
    while time.time() < deadline:
        with appmod._risk_lock:
            state = dict(appmod.RISK_RUNS.get(key) or {})
        if state.get("state") != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("the analysis never finished")


def _register_run(client, url=DEMO):
    return _finish(client, client.post(
        "/agents/input-credit",
        files={"register": ("r.csv", SAMPLE_REGISTER.encode(), "text/csv")},
        data={"use_agent": "no"},
        follow_redirects=False).headers["location"].split("key=")[-1])


def test_the_tabs_are_the_three_ingestion_routes():
    from merchant.nav import AGENT_ROUTES

    tabs = AGENT_ROUTES["gst_itc"].tabs
    assert [t.label for t in tabs] == ["Demo Mode", "Without API", "With API"]
    assert [t.slug for t in tabs] == ["", "without-api", "with-api"]


def test_demo_mode_asks_for_nothing(shop):
    """Both halves are generated, so there is nothing to upload."""
    page = shop.get(DEMO).text
    assert "Generate &amp; analyse demo data" in page
    assert 'type="file"' not in page
    assert "Do not act on any of it against a real supplier" in page


def test_without_api_asks_for_history_then_the_register(shop):
    page = shop.get(WITHOUT_API).text
    assert "Step 1" in page
    assert 'name="history"' in page
    assert "one-time" in page
    assert "Generate &amp; analyse demo data" not in page


def test_without_api_spells_out_the_portal_path(shop):
    """
    A merchant who has never downloaded a GSTR-2B will not find it from "get
    your GSTR-2B" - the JSON is four clicks deep and the tile offers an Excel
    first, which is the wrong file.
    """
    page = shop.get(WITHOUT_API).text
    for step in ("gst.gov.in", "Services", "Returns", "Returns Dashboard",
                 "Select Financial Year", "Search", "GSTR-2B Tile",
                 "Download", "Generate JSON File to Download"):
        assert step in page, step


def test_with_api_offers_the_connection_then_the_register(shop):
    page = shop.get(WITH_API).text
    assert "Connect a GST filing-status API" in page
    assert 'name="url_template"' in page

    shop.post("/agents/input-credit/filing-api",
              data={"url_template": "https://p.test/{gstin}"})
    connected = shop.get(WITH_API).text
    assert "Connected GST API" in connected
    assert 'name="register"' in connected
    # No history box: it is fetched, not asked for.
    assert 'name="history"' not in connected


def test_only_the_demo_tab_offers_generated_data(shop):
    for url in (WITHOUT_API, WITH_API):
        assert "Generate &amp; analyse demo data" not in shop.get(url).text


# --- what each tab actually runs -----------------------------------------

def test_demo_mode_generates_both_halves_and_scores_them(shop):
    key = shop.post("/agents/input-credit/demo", data={"use_agent": "no"},
                    follow_redirects=False
                    ).headers["location"].split("key=")[-1]
    state = _finish(shop, key)

    assert state["state"] == "done", state
    payload = state["payload"]
    assert payload["portfolio"]["history_source"] == "simulated"
    assert payload["portfolio"]["history_is_demo"] is True
    from merchant.purchase_import import parse

    expected = len(parse(SAMPLE_REGISTER.encode(), "r.csv").groups)
    assert len(payload["suppliers"]) == expected
    assert all(s["compliance_grid"] for s in payload["suppliers"])


def test_without_api_joins_the_register_to_the_uploaded_history(shop):
    shop.post("/agents/input-credit/history",
              files={"history": ("h.csv", sample_filing_history().encode(),
                                 "text/csv")})
    state = _register_run(shop)

    assert state["state"] == "done", state
    assert state["payload"]["portfolio"]["history_source"] == "file"
    assert state["payload"]["portfolio"]["history_is_demo"] is False


def test_with_api_fetches_history_for_the_registers_gstins(shop, monkeypatch):
    """
    The register's GSTINs are what gets looked up - nothing else is uploaded.

    Asserted on which GSTINs the provider was asked about, because that is the
    claim the tab makes: extract the suppliers from the register, then fetch
    each one.
    """
    import merchant.risk_pipeline as pipeline
    from engine.gst.filing_history import history_for
    from merchant.gstin_lookup import FilingStatusApi

    asked = []

    class Response:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def http(_method, url, **_kw):
        from engine.gst.filing_history import as_gstn_payload

        gstin = url.rsplit("/", 1)[-1]
        asked.append(gstin)
        return Response(as_gstn_payload(history_for(gstin)))

    real = pipeline.history_service_for

    def with_fake_http(led, business_id, **kw):
        return real(led, business_id, **{**kw, "http": http})

    # merchant.app imports this inside the function it runs in, so patching
    # the pipeline module's attribute is what actually takes effect.
    monkeypatch.setattr(pipeline, "history_service_for", with_fake_http)

    shop.post("/agents/input-credit/filing-api",
              data={"url_template": "https://p.test/returns/{gstin}"})
    state = _register_run(shop)

    assert state["state"] == "done", state
    assert state["payload"]["portfolio"]["history_source"] == "api"

    from merchant.purchase_import import parse

    expected = {g.supplier_gstin
                for g in parse(SAMPLE_REGISTER.encode(), "r.csv").groups}
    assert set(asked) == expected


def test_a_register_with_no_history_refuses_rather_than_simulating(shop):
    """
    The guardrail the whole three-tab split rests on.

    A register uploaded on Without API before any history exists has nothing
    to be scored against. Falling back to the simulator would put generated
    filing records against real companies' names and call it a risk
    assessment.
    """
    state = _register_run(shop)

    assert state["state"] == "failed"
    assert "would not be a risk assessment" in state["phase"]
    assert "payload" not in state


def test_the_api_outranks_an_uploaded_history(shop):
    """More current, and it can see payment - which GSTR-2B usually cannot."""
    shop.post("/agents/input-credit/history",
              files={"history": ("h.csv", sample_filing_history().encode(),
                                 "text/csv")})
    shop.post("/agents/input-credit/filing-api",
              data={"url_template": "https://p.test/{gstin}"})

    import merchant.app as appmod
    from merchant.risk_pipeline import history_service_for

    with appmod.ledger(None) as led:
        biz = led.businesses.all()[0]["business_id"]
        led.business_id = biz
        assert history_service_for(led, biz).source == "api"


def test_the_settlement_connector_does_not_decide_gst_history(shop):
    """
    Which settlement source a business uses says nothing about how it gets GST
    filing history. Keying one off the other was a coupling nobody could have
    predicted from the screen.
    """
    import merchant.app as appmod
    from merchant.risk_pipeline import history_service_for

    shop.post("/agents/input-credit/history",
              files={"history": ("h.csv", sample_filing_history().encode(),
                                 "text/csv")})
    with appmod.ledger(None) as led:
        biz = led.businesses.all()[0]["business_id"]
        led.business_id = biz
        assert history_service_for(led, biz).source == "file"


def test_every_tab_reaches_the_identical_dashboard(shop):
    """
    The strict requirement: the agent must not be able to tell which tab a run
    came from, and neither should the dashboard.

    The same register is run twice - once off generated history, once off an
    uploaded file - and the payloads must match apart from the provenance
    fields that exist precisely to differ.
    """
    demo = _finish(shop, shop.post(
        "/agents/input-credit/demo", data={"use_agent": "no"},
        follow_redirects=False).headers["location"].split("key=")[-1])
    assert demo["state"] == "done", demo

    shop.post("/agents/input-credit/history",
              files={"history": ("h.csv", sample_filing_history().encode(),
                                 "text/csv")})
    uploaded = _register_run(shop)
    assert uploaded["state"] == "done", uploaded

    assert demo["payload"]["portfolio"]["history_source"] == "simulated"
    assert uploaded["payload"]["portfolio"]["history_source"] == "file"
    assert _without_provenance(demo["payload"]) == \
        _without_provenance(uploaded["payload"])


def test_a_demo_run_does_not_badge_the_agent_as_live(shop):
    """
    Regression. The badge read "Live data" whenever a purchase row was marked
    "imported", which was sound while only a merchant's upload could produce
    one - and wrong the moment the demo button began generating a register and
    storing it the same way. A business on the simulator was badged live on
    the strength of data the platform had invented.
    """
    _finish(shop, shop.post("/agents/input-credit/demo",
                            data={"use_agent": "no"}, follow_redirects=False
                            ).headers["location"].split("key=")[-1])

    page = shop.get("/agents/input-credit").text
    assert "Demo data" in page
    assert "Live data" not in page


def test_live_data_is_badged_live_once_a_real_register_is_in(biz):
    """The other half: the badge has to be reachable, or it means nothing."""
    biz.post("/agents/input-credit/history",
             files={"history": ("h.csv", sample_filing_history().encode(),
                                "text/csv")})
    _finish(biz, biz.post(
        "/agents/input-credit",
        files={"register": ("r.csv", SAMPLE_REGISTER.encode(), "text/csv")},
        data={"use_agent": "no"},
        follow_redirects=False).headers["location"].split("key=")[-1])

    page = biz.get("/agents/input-credit").text
    assert "Live data" in page
    assert "Demo data" not in page


# --- the demo has to demonstrate ------------------------------------------
#
# Both of these pin a defect that shipped. The register was six suppliers and
# the personas are weighted the way real ones are - about one in ten defaults -
# so the demo showed four clean suppliers and one late filer: a working table,
# and no evidence the product does anything. And the demo stored purchases
# with no GSTR-2B at all, so every invoice reconciled as "absent from GSTR-2B"
# and the four discrepancies the engine can actually tell apart never appeared.
#
# A demo that can only produce one finding demonstrates almost nothing, so the
# coverage is asserted rather than left to the weighting.

def test_the_demo_register_covers_every_risk_pattern():
    from collections import Counter

    from engine.gst.filing_history import history_for
    from engine.gst.risk import (PATTERN_CLEAN, PATTERN_DEFAULTER,
                                 PATTERN_ERRATIC, PATTERN_LATE, PATTERN_THIN)
    from merchant.purchase_import import parse

    groups = parse(SAMPLE_REGISTER.encode(), "r.csv").groups
    assert len(groups) >= 20, "a demo this small can contain nothing to find"

    found = Counter(profile(history_for(g.supplier_gstin)).pattern
                    for g in groups)
    for pattern in (PATTERN_CLEAN, PATTERN_LATE, PATTERN_DEFAULTER,
                    PATTERN_ERRATIC, PATTERN_THIN):
        assert found[pattern] >= 1, f"{pattern} never appears: {found}"

    # And most suppliers are fine, because most suppliers are fine. A demo
    # where everybody is a defaulter is as useless as one where nobody is.
    assert found[PATTERN_CLEAN] > found[PATTERN_DEFAULTER]


def test_the_demo_run_covers_every_reconciliation_discrepancy(shop):
    """
    The half that was missing entirely.

    The demo must generate what the suppliers reported as well as what the
    merchant bought, because the gap between those two IS the reconciliation.
    Without it every invoice came back absent_from_2b.
    """
    from collections import Counter

    import merchant.app as appmod
    from engine.gst.detector import detect_batch

    key = shop.post("/agents/input-credit/demo", data={"use_agent": "no"},
                    follow_redirects=False
                    ).headers["location"].split("key=")[-1]
    assert _finish(shop, key)["state"] == "done"

    with appmod.ledger(None) as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] > 0
        variances = detect_batch(led.build_itc_batch())

    found = Counter(v.signals[0].kind if v.signals else "CLEAN"
                    for v in variances)
    for kind in ("matched_exactly", "absent_from_2b",
                 "absent_but_similar_elsewhere", "tax_short_in_2b",
                 "filed_in_later_period"):
        assert found[kind] >= 1, f"{kind} never appears: {dict(found)}"


def test_a_real_upload_never_gets_a_generated_gstr2b(shop):
    """
    The line the demo must not cross.

    For a real register the other half has to be the merchant's own GSTR-2B.
    Manufacturing it would be inventing the evidence the product exists to
    check against - a reconciliation that always balances because both sides
    came from the same generator.
    """
    import merchant.app as appmod

    shop.post("/agents/input-credit/history",
              files={"history": ("h.csv", sample_filing_history().encode(),
                                 "text/csv")})
    state = _register_run(shop)
    assert state["state"] == "done", state

    with appmod.ledger(None) as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 0


def test_the_reconciliation_is_reachable_from_the_results(shop):
    """
    It is not a tab any more, by request - but a page nothing links to is a
    page nobody finds. The dashboard says what the other half answers and
    links to it, because "who your credit depends on" and "did this month's
    invoices match GSTR-2B" are genuinely different questions about the same
    suppliers.
    """
    key = shop.post("/agents/input-credit/demo", data={"use_agent": "no"},
                    follow_redirects=False
                    ).headers["location"].split("key=")[-1]
    assert _finish(shop, key)["state"] == "done"

    page = shop.get(f"/agents/input-credit?key={key}").text
    assert 'href="/agents/input-credit/reconciliation"' in page
    assert "The other half of this agent" in page

    # And the link goes somewhere that works.
    assert shop.get("/agents/input-credit/reconciliation").status_code == 200


# --- the agent can drill past the twelve-month summary now -----------------
#
# render() hands the agent totals plus the last twelve periods. These tools
# let it see the whole thirty-six-month history and this month's statutory
# clocks before it writes the sentence explaining a supplier's record - the
# same shape as the other two agents' tools, applied to this one's real gap.

def test_full_filing_history_shows_more_than_the_summary():
    from agent.risk_tools import build_tools

    history = history_for("27AAAAA0000A1Z5")
    tools = {t.name: t for t in build_tools(history=history)}
    assert set(tools) == {"full_filing_history"}

    out = json.loads(tools["full_filing_history"].call({}))
    assert out["total_periods"] == len(history.months)
    assert out["total_periods"] > 12, "not more than the summary already gave"


def test_statutory_clocks_are_offered_only_when_there_are_invoices():
    from agent.risk_tools import build_tools

    assert build_tools(history=None, clocks={}) == []
    assert build_tools(history=None, clocks=None) == []

    clocks = {"rule_37_days_left": 12, "claim_days_left": 90, "invoices": [1]}
    tools = {t.name: t for t in build_tools(clocks=clocks)}
    assert set(tools) == {"statutory_clocks"}
    assert json.loads(tools["statutory_clocks"].call({})) == clocks


def test_both_tools_together_when_both_are_available():
    from agent.risk_tools import build_tools

    history = history_for("27AAAAA0000A1Z5")
    clocks = {"rule_37_days_left": 5, "invoices": [1]}
    tools = {t.name: t for t in build_tools(history=history, clocks=clocks)}
    assert set(tools) == {"full_filing_history", "statutory_clocks"}


def test_the_tools_are_read_only():
    from agent.risk_tools import build_tools

    history = history_for("27AAAAA0000A1Z5")
    before = [(m.period, m.gstr1_filed, m.gstr3b_filed) for m in history.months]
    tools = {t.name: t for t in build_tools(history=history,
                                            clocks={"invoices": [1]})}
    for t in tools.values():
        t.call({})
    after = [(m.period, m.gstr1_filed, m.gstr3b_filed) for m in history.months]
    assert before == after


def test_the_agent_still_answers_without_history_or_clocks():
    """Tools are an addition to a complete answer, never a precondition."""
    from agent.risk_agent import ClaudeRiskAgent

    class Refuses:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**kw):
                    assert kw["tools"] == [], "no history/clocks means no tools"
                    raise ConnectionError("down")

    prof = profile(history_for("27AAAAA0000A1Z5"))
    verdict = ClaudeRiskAgent(client=Refuses()).judge(
        prof, "Anand Textiles", 100_00, 10_00, [])
    assert verdict.action == recommended_action(prof)
    assert verdict.tool_calls == []


def test_a_tool_run_reports_its_full_cost():
    """
    Same class of bug shipped twice already in this project: reading usage
    off only the last turn of a multi-turn tool call.
    """
    from agent.risk_agent import ClaudeRiskAgent, RiskJudgment

    def _judgment():
        return RiskJudgment(pattern=PATTERN_CLEAN, action="safe_to_pay",
                            headline="h", reasoning="r")

    class Turn:
        def __init__(self, i, o, last=False):
            self.usage = type("U", (), {"input_tokens": i, "output_tokens": o,
                                        "cache_read_input_tokens": 0})()
            self.content = []
            self.parsed_output = _judgment() if last else None

    turns = [Turn(1500, 200), Turn(1700, 180, last=True)]

    class Runner:
        def __iter__(self):
            return iter(turns)

    class Client:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**_kw):
                    return Runner()

    prof = profile(history_for("27AAAAA0000A1Z5"))
    verdict = ClaudeRiskAgent(client=Client()).judge(
        prof, "Anand Textiles", 100_00, 10_00, [],
        history=history_for("27AAAAA0000A1Z5"))
    assert verdict.input_tokens == 3200
    assert verdict.output_tokens == 380


def test_what_it_checked_reaches_the_drawer(shop):
    """A recommendation that read the full history and one that saw only the
    summary produce the same sentence. The drawer has to tell them apart."""
    import merchant.app as appmod

    key, state = _analyse(shop)
    payload = state["payload"]
    payload["suppliers"][0]["tool_calls"] = [
        "full_filing_history", "statutory_clocks"]
    with appmod._risk_lock:
        appmod.RISK_RUNS[key]["payload"] = payload

    page = shop.get(f"/agents/input-credit?key={key}").text
    assert "Before deciding, it checked" in page
    assert "read their full filing history" in page
    assert "checked the Rule 37" in page


def test_the_sample_register_follows_the_calendar():
    """
    Regression, found by the suite going red at midnight on 1 September 2026
    with no code change.

    build_itc_batch passes period=current_period() - today's month - and the
    detector raises filed_in_later_period only when a supplier's filed period
    is later than that. With the sample register's dates fixed to August, a
    late filing landed in September: genuinely later during August, merely
    current from the 1st onward, so an entire category of finding silently
    stopped existing. The dates have to track the run date.
    """
    from datetime import date

    from merchant.purchase_import import _sample_register

    for when in (date(2026, 9, 15), date(2027, 1, 3), date(2027, 2, 8)):
        rows = [l for l in _sample_register(when).splitlines()[1:] if l]
        months = {l.split(",")[3][:7] for l in rows}
        assert months == {f"{when.year}-{when.month:02d}"}, (
            f"sample register did not follow {when}: {months}")

        # February has no 30th. Clamping must shorten a day, never invent one.
        days = [int(l.split(",")[3].rsplit("-", 1)[1]) for l in rows]
        import calendar
        assert max(days) <= calendar.monthrange(when.year, when.month)[1]
