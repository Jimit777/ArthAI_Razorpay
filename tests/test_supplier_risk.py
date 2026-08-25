"""
Tests for the supplier risk engine.

The thing being protected throughout: every number on that page is computed,
and the agent has nowhere to put one. The specification for this feature asked
the model to calculate the late-filing percentage and return a risk probability
as a float; both are arithmetic, and a model that is occasionally, silently
wrong about a figure destroys the only thing this product sells.
"""

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
                                       GSTR3B_DUE_DAY, Persona, history_for)
from engine.gst.risk import (MIN_PERIODS, PATTERN_CLEAN,  # noqa: E402
                             PATTERN_DEFAULTER, PATTERN_LATE, PATTERN_THIN,
                             exposure_at_risk, profile)
from merchant.purchase_import import SAMPLE_REGISTER, parse  # noqa: E402
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
                       registration_status="cancelled")
    assert prof.trust_score <= DEAD_REGISTRATION_CAP


def test_thin_history_is_not_scored_as_risk():
    from engine.gst.risk import RiskProfile

    prof = RiskProfile(gstin="X", periods=2, gstr1_filed=2, gstr3b_filed=0)
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
    import merchant.app as appmod

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
    page = shop.get("/agents/input-credit").text
    assert "simulated" in page
    assert "GSP" in page


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
            registration_status=status)
        assert recommended_action(spotless_but_dead) != ACT_SAFE, status


def test_thin_history_recommends_caution_not_confidence():
    from engine.gst.risk import ACT_WATCH, RiskProfile, recommended_action

    thin = RiskProfile(gstin="X", periods=2, gstr1_filed=2, gstr3b_filed=2)
    assert recommended_action(thin) == ACT_WATCH


def test_one_missed_payment_is_enough_to_stop_saying_safe():
    from engine.gst.risk import ACT_SAFE, RiskProfile, recommended_action

    almost = RiskProfile(gstin="X", periods=36, gstr1_filed=36,
                         gstr3b_filed=35, sold_but_did_not_pay=1)
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
                           gstr3b_filed=36)
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

    spotless = dict(gstin="X", periods=36, gstr1_filed=36, gstr3b_filed=36)
    assert recommended_action(
        RiskProfile(**spotless, registration_status="cancelled")) == ACT_STOP
    assert recommended_action(
        RiskProfile(**spotless, registration_status="suspended")) == ACT_HOLD


def test_a_settled_pattern_of_default_means_stop():
    from engine.gst.risk import ACT_STOP, RiskProfile, recommended_action

    terminal = RiskProfile(
        gstin="X", periods=36, gstr1_filed=36, gstr3b_filed=8,
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
