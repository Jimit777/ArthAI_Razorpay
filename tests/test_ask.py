"""
Tests for the question box.

A free-text answer has none of the constraints a classification has: no
eleven-code enum, no fixed schema. So the guardrails that matter here are the
ones about what the agent is allowed to KNOW and allowed to SAY, and both are
tested without calling the API.

The sharpest one: if a merchant asks "how much am I owed in total?", that total
must already exist in the briefing as a formatted string. The alternative is a
model adding up its own evidence - which is arithmetic, which is the one thing
this system does not let a model do, and which would eventually be wrong in
front of someone about to email their gateway.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classifier import Verdict, unverified_figures  # noqa: E402
from engine.detector import detect_batch  # noqa: E402
from engine.expected_value import rupees  # noqa: E402
from engine.gate import gate_batch  # noqa: E402
from merchant.ask import MAX_QUESTION, SYSTEM, Answer, ask, build_briefing  # noqa: E402
from merchant.gateway import Behaviour  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402


@pytest.fixture
def led(tmp_path):
    boot = Ledger(tmp_path / "ask.db")
    business_id = boot.businesses.create("Meera's Boutique")
    boot.close()

    ledger = Ledger(tmp_path / "ask.db", business_id)
    ledger.set_behaviour(Behaviour.CARD_RATE_ON_UPI)
    ledger.capture_payment(ledger.create_order(162_700, "Silk scarf"), "upi")
    ledger.capture_payment(ledger.create_order(350_000, "Kurta set"),
                           "card", "visa", "credit")
    # Large enough that the overcharge clears the Rs 250 review threshold, so
    # the briefing has a held finding in it to describe.
    ledger.capture_payment(ledger.create_order(5_000_000, "Bridal lehenga"), "upi")

    batch = ledger.build_settlement(ledger.rate_card())
    run_id = ledger.commit_settlement(batch)
    variances = detect_batch(batch)
    verdicts = [Verdict(payment_id=v.payment_id,
                        exception_code="ZERO_MDR_VIOLATION", action="dispute",
                        confidence=0.95, reasoning="Network MDR on a zero-MDR rail.",
                        rule_cited="rule 1 - PSS Act s.10A",
                        dispute_text="Please issue a credit note.")
                for v in variances if v.needs_agent]
    decisions = gate_batch(variances, verdicts, ledger.rate_card())
    ledger.store.save_findings(run_id, decisions, variances, verdicts)

    yield ledger
    ledger.close()


# --- what it is allowed to know -----------------------------------------

def test_the_briefing_contains_only_this_businesses_data(tmp_path):
    """
    The agent answers from the briefing and nothing else. If another business's
    figures could reach it, a merchant could ask about someone else's books.
    """
    boot = Ledger(tmp_path / "two.db")
    a = boot.businesses.create("Ours")
    b = boot.businesses.create("Theirs")
    boot.close()

    la, lb = Ledger(tmp_path / "two.db", a), Ledger(tmp_path / "two.db", b)
    lb.capture_payment(lb.create_order(999_999, "Their secret sale"), "upi")
    lb.commit_settlement(lb.build_settlement(lb.rate_card()))

    briefing = build_briefing(la)
    assert "Ours" in briefing
    assert "Theirs" not in briefing
    assert "9,999.99" not in briefing
    la.close(); lb.close()


def test_the_briefing_carries_the_rate_card_with_its_sources(led):
    """
    A merchant about to argue with their gateway needs to know what they are
    arguing from. An answer citing "your contract" without the circular is not
    something anyone can act on.
    """
    briefing = build_briefing(led)
    assert "PSS Act s.10A" in briefing
    assert "RBI circular RBI/2017-18/105" in briefing
    assert "capped by regulation" in briefing
    assert "GST: 18% of the fee, never of the sale" in briefing


def test_the_briefing_says_when_a_finding_is_held_for_review(led):
    """
    Presenting a held finding as settled would have a merchant claiming money
    the system has not finished deciding about.
    """
    briefing = build_briefing(led)
    assert "HELD FOR HUMAN REVIEW" in briefing


def test_an_empty_business_gets_an_honest_briefing(tmp_path):
    boot = Ledger(tmp_path / "empty.db")
    biz = boot.businesses.create("Brand New")
    boot.close()
    led = Ledger(tmp_path / "empty.db", biz)

    briefing = build_briefing(led)
    assert "None yet" in briefing
    assert "Nothing has been settled or audited" in briefing
    led.close()


# --- the arithmetic ban, which is the whole point ------------------------

def test_every_total_is_precomputed_in_the_briefing(led):
    """
    The model must never need to add anything up. Each total it might be asked
    for is already there, formatted, with an instruction not to recompute it.
    """
    briefing = build_briefing(led)
    assert "already computed - quote these, never recompute them" in briefing
    for label in ("Total identified as recoverable:",
                  "Records audited in total:",
                  "Findings that need action:",
                  "Held for human review:"):
        assert label in briefing


def test_the_recoverable_total_matches_what_the_database_says(led):
    """
    The precomputed figure has to be right, or the ban just moves the error
    from the model to us.
    """
    briefing = build_briefing(led)
    expected = sum(led.store.totals(r["run_id"])["recoverable_paise"]
                   for r in led.settlements())
    assert f"Total identified as recoverable: {rupees(expected)}" in briefing


def test_a_quoted_total_passes_the_figure_check(led):
    """The answer that started this test file: Rs 311.97 was quoted, not derived."""
    briefing = build_briefing(led)
    total = sum(led.store.totals(r["run_id"])["recoverable_paise"]
                for r in led.settlements())
    answer = f"In total, {rupees(total)} is identified as recoverable."
    assert unverified_figures(answer, briefing) == []


def test_a_derived_total_is_caught(led):
    """And a number the model worked out for itself is not."""
    briefing = build_briefing(led)
    assert unverified_figures("You are owed Rs 4,912.55 in total.", briefing) \
        == ["4912.55"]


def test_the_system_prompt_forbids_arithmetic():
    assert "DO NOT DO ARITHMETIC" in SYSTEM
    assert "Do not derive it" in SYSTEM
    assert "cannot change anything" in SYSTEM


# --- answering -----------------------------------------------------------

class _FakeClient:
    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        block = type("B", (), {"type": "text", "text": self._text})()
        usage = type("U", (), {"input_tokens": 40, "output_tokens": 120,
                               "cache_read_input_tokens": 900})()
        return type("R", (), {"content": [block], "usage": usage})()


def test_a_good_answer_comes_back_trustworthy(led):
    total = rupees(sum(led.store.totals(r["run_id"])["recoverable_paise"]
                       for r in led.settlements()))
    answer = ask(led, "How much am I owed?",
                 client=_FakeClient(f"You are owed {total}."))
    assert answer.trustworthy
    assert answer.invented_figures == []
    assert answer.output_tokens == 120


def test_an_answer_with_an_invented_figure_is_flagged_not_hidden(led):
    """
    Shown with the problem named, rather than suppressed. A merchant who asked a
    question deserves to see the answer and be told which part not to trust.
    """
    answer = ask(led, "How much am I owed?",
                 client=_FakeClient("You are owed Rs 8,888.88."))
    assert not answer.trustworthy
    assert answer.invented_figures == ["8888.88"]
    assert answer.text, "the answer is still returned"


def test_an_api_failure_is_reported_not_swallowed(led):
    class _Broken:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("credit balance too low")

    answer = ask(led, "How much am I owed?", client=_Broken())
    assert not answer.trustworthy
    assert "credit balance" in answer.error
    assert answer.text == ""


def test_an_empty_question_is_refused(led):
    assert ask(led, "   ").error == "no question was asked"


def test_a_very_long_question_is_truncated_not_rejected(led):
    answer = ask(led, "why " * 400, client=_FakeClient("Because."))
    assert len(answer.question) <= MAX_QUESTION
    assert answer.trustworthy


def test_the_briefing_is_sent_as_a_cached_prefix(led):
    """
    The briefing is the same for every question about the same books. Sending
    it uncached would pay full price for it on every question asked.
    """
    client = _FakeClient("Fine.")
    ask(led, "Anything wrong?", client=client)
    system = client.kwargs["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Briefing for Meera's Boutique" in system[0]["text"]


def test_the_question_is_the_only_thing_outside_the_cached_prefix(led):
    client = _FakeClient("Fine.")
    ask(led, "Why is my payout short?", client=client)
    assert client.kwargs["messages"] == [
        {"role": "user", "content": "Why is my payout short?"}]


# --- the page ------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


def _ready(client):
    """An account, a business, a settlement and an audit - so there is something
    to ask about."""
    client.post("/signup", data={"email": "owner@x.in",
                                 "password": "a-good-password"})
    client.post("/businesses", data={"name": "Boutique"})
    client.post("/sources/simulator")
    client.post("/settings/gateway", data={"behaviour": "card_rate_on_upi"})
    client.post("/sale", data={"rupees": "1627.00", "instrument": "upi"})
    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    client.post(f"/audit/{run_id}", data={})       # rules only, no API call
    import time
    for _ in range(50):
        if client.get(f"/audit/{run_id}/status").json()["state"] != "running":
            break
        time.sleep(0.1)
    return run_id


def test_with_nothing_audited_it_says_so_instead_of_offering_a_box(client):
    """
    The agent answers from findings that exist. An input box over an empty
    briefing invites a question it can only refuse.
    """
    client.post("/signup", data={"email": "new@x.in", "password": "a-good-password"})
    client.post("/businesses", data={"name": "Empty Co"})
    client.post("/sources/simulator")

    page = client.get("/ask").text
    assert "so there has" in page and "something in them first" in page
    assert "Audit a settlement" in page
    assert 'id="ask-input"' not in page


def test_the_page_states_exactly_what_the_agent_can_see(client):
    """
    "It can see your books" is a promise. Saying how many settlements and
    findings makes it checkable.
    """
    _ready(client)
    page = client.get("/ask").text
    assert "audited settlement(s)" in page
    assert "with findings" in page
    assert "and nothing else" in page


def test_asking_shows_the_question_landing_before_the_answer_arrives(client):
    """
    An answer takes ten to fifteen seconds. A form POST leaves the page blank
    for all of it, which reads as a hang.
    """
    _ready(client)
    page = client.get("/ask").text
    assert "Reading your settlements" in page
    assert "qa-dots" in page
    assert "Accept': 'application/json" in page or '"Accept"' in page


def test_the_json_and_form_paths_render_the_same_answer(client, monkeypatch):
    """
    JavaScript gets JSON, everyone else gets a redirect - but both go through
    the same renderer, so the two cannot drift into different pages.
    """
    from merchant.ask import Answer
    import merchant.app as appmod

    canned = Answer(question="How much am I owed?",
                    text="You are owed Rs 17.28.\nAcross one finding.",
                    latency_ms=9000, output_tokens=120)
    monkeypatch.setattr(appmod, "ask", lambda *a, **kw: canned, raising=False)
    import merchant.ask as askmod
    monkeypatch.setattr(askmod, "ask", lambda *a, **kw: canned)

    _ready(client)
    as_json = client.post("/ask", data={"question": "How much am I owed?"},
                          headers={"Accept": "application/json"})
    assert as_json.status_code == 200
    html = as_json.json()["html"]
    assert "You are owed Rs 17.28." in html
    assert html.count("<p>") == 2, "paragraphs are preserved"

    rendered = client.get("/ask").text
    assert "You are owed Rs 17.28." in rendered


def test_an_unverified_figure_is_flagged_on_the_page(client, monkeypatch):
    """Shown with the problem named, not suppressed."""
    from merchant.ask import Answer
    import merchant.ask as askmod

    monkeypatch.setattr(askmod, "ask", lambda *a, **kw: Answer(
        question="How much?", text="You are owed Rs 9,999.99.",
        invented_figures=["9999.99"], latency_ms=8000))

    _ready(client)
    client.post("/ask", data={"question": "How much?"})
    page = client.get("/ask").text
    assert "Check this" in page
    assert "9999.99" in page
    assert "Rs 9,999.99" in page, "the answer itself is still shown"
