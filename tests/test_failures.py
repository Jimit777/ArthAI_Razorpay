"""
What a merchant sees when the agent cannot be reached.

Every agent here falls back to arithmetic when a model call fails - that was
already true and is the point of the split in CLAUDE.md section 2. What was
not true is that the failure was LEGIBLE, or that it was survivable.

Two defects these tests pin down:

  A missing API key raised a TypeError out of the SDK at request time, which
  escaped the classifiers' `except (APIStatusError, APIConnectionError)` and
  took down a whole run whose scores were already computed. Anyone who cloned
  this repo and pressed the demo button hit it, because the agent checkbox is
  ticked by default.

  A failed call put the raw exception into merchant-facing prose, so a finance
  screen displayed a Python traceback with a JSON blob in it - next to figures
  that were all correct.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import failures  # noqa: E402
from engine.gst.filing_history import (SimulatedHistoryProvider,  # noqa: E402
                                       SupplierHistoryService)
from merchant.purchase_import import SAMPLE_REGISTER, parse  # noqa: E402
from merchant.risk_pipeline import run  # noqa: E402

NO_KEY = ('TypeError: "Could not resolve authentication method. Expected one '
          'of api_key, auth_token, or credentials to be set."')
NO_CREDITS = ("BadRequestError: Error code: 400 - {'type': 'error', 'error': "
              "{'type': 'invalid_request_error', 'message': 'Your credit "
              "balance is too low to access the Anthropic API.'}}")
BAD_KEY = "AuthenticationError: Error code: 401 - invalid x-api-key"


@pytest.mark.parametrize("error,expected", [
    (NO_KEY, failures.NOT_CONFIGURED),
    (NO_CREDITS, failures.NO_CREDITS),
    (BAD_KEY, failures.BAD_KEY),
    ("RateLimitError: rate limit exceeded", failures.TRANSIENT),
    ("APIConnectionError: Connection error.", failures.UNKNOWN),
    (None, failures.UNKNOWN),
])
def test_each_failure_is_recognised(error, expected):
    assert failures.kind(error) == expected


def test_a_missing_key_is_not_mistaken_for_a_bad_one():
    """
    The two are one word apart in the SDK's message and the fixes have nothing
    in common. Telling someone to rotate a key they never set is worse than
    saying nothing.
    """
    assert failures.kind(NO_KEY) == failures.NOT_CONFIGURED
    assert failures.kind(BAD_KEY) == failures.BAD_KEY
    assert "ANTHROPIC_API_KEY is set" in failures.explain(NO_KEY)
    assert "rejected" in failures.explain(BAD_KEY)


def test_no_credits_is_not_reported_as_a_key_problem():
    """The wall this project hit twice. 'Rotate your key' does nothing here."""
    assert "no credits left" in failures.explain(NO_CREDITS)
    assert "key" not in failures.explain(NO_CREDITS).lower()


@pytest.mark.parametrize("error", [NO_KEY, NO_CREDITS, BAD_KEY])
def test_the_hopeless_failures_stop_the_run_asking(error):
    """Fifty identical failures cost no tokens and a minute of attention."""
    assert failures.is_fatal(error)


def test_a_transient_failure_does_not_stop_the_run():
    assert not failures.is_fatal("RateLimitError: rate limit exceeded")


def test_every_explanation_says_the_analysis_survived():
    """
    The second sentence is always the reassurance, because it is always true.

    A person reading "the agent could not be reached" needs to know within one
    sentence that the numbers beside it are still good, or they will conclude
    the product is broken and stop reading.
    """
    for state in (failures.NOT_CONFIGURED, failures.NO_CREDITS,
                  failures.BAD_KEY, failures.NO_ACCESS, failures.TRANSIENT,
                  failures.UNKNOWN):
        text = failures.EXPLANATION[state]
        assert "computed without" in text
        assert text.endswith(".")


# --- the whole run, with no key at all -----------------------------------

def test_a_run_with_no_api_key_completes_instead_of_crashing(monkeypatch):
    """
    Regression, and the one that mattered most.

    The agent checkbox is ticked by default, so this is what a person who
    clones the repo and presses the demo button gets. It used to be a
    TypeError traceback; it is now a complete analysis with the explanation
    column missing.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    imported = parse(SAMPLE_REGISTER.encode(), "register.csv")
    portfolio = run(imported, use_agent=True,
                    history=SupplierHistoryService(SimulatedHistoryProvider()))
    payload = portfolio.as_dict()

    assert payload["portfolio"]["suppliers"] == 6
    assert payload["portfolio"]["failed_calls"] == 6
    assert payload["portfolio"]["usage"]["usd"] == 0

    for supplier in payload["suppliers"]:
        # Everything that is arithmetic survived.
        assert supplier["trust_score"] > 0
        assert supplier["pattern"]
        assert supplier["action"]
        assert len(supplier["compliance_grid"]) == 36
        # And the explanation says why it is missing, in English.
        assert "No ANTHROPIC_API_KEY is set" in supplier["reasoning"]
        assert "TypeError" not in supplier["reasoning"]
        assert "{'type'" not in supplier["reasoning"]


def test_the_raw_exception_is_kept_for_whoever_is_debugging(monkeypatch):
    """Legible for the merchant, complete for the developer. Both, not one."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from agent.risk_agent import ClaudeRiskAgent
    from engine.gst.filing_history import history_for
    from engine.gst.risk import profile

    prof = profile(history_for("24FJAMH3956X5ZJ"))
    verdict = ClaudeRiskAgent().judge(prof, "Anand Textiles", 100, 10, [])

    assert "TypeError" in (verdict.error or "")
    assert "No ANTHROPIC_API_KEY is set" in verdict.reasoning


def test_a_fatal_failure_stops_the_agent_asking_again(monkeypatch):
    """
    Once one call has failed hopelessly, later suppliers skip the round trip.

    Worth being precise about what this does and does not do. The SDK does not
    reject a missing key when the client is BUILT - it raises at request time -
    so the first call always goes out, and calls already in flight on other
    threads go out too. What is prevented is the long tail: on a register of
    fifty suppliers the first few fail and the remaining forty-odd are never
    attempted.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from agent.risk_agent import ClaudeRiskAgent
    from engine.gst.filing_history import history_for
    from engine.gst.risk import profile

    agent = ClaudeRiskAgent()
    prof = profile(history_for("24FJAMH3956X5ZJ"))

    assert agent._fatal is None
    first = agent.judge(prof, "Anand Textiles", 100, 10, [])
    assert first.error
    assert agent._fatal is not None, "a hopeless failure should latch"

    # The second never reaches the transport - it returns on the latch, and
    # its latency is the giveaway.
    second = agent.judge(prof, "Coimbatore Yarns", 100, 10, [])
    assert second.error == agent._fatal
    assert second.latency_ms <= first.latency_ms
    assert "No ANTHROPIC_API_KEY is set" in second.reasoning


def test_a_transient_failure_does_not_latch(monkeypatch):
    """Only the hopeless ones stop the run. A blip must not skip the rest."""
    from agent.risk_agent import ClaudeRiskAgent
    from engine.gst.filing_history import history_for
    from engine.gst.risk import profile

    class Flaky:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**_kw):
                    raise ConnectionError("rate limit exceeded")

    agent = ClaudeRiskAgent(client=Flaky())
    prof = profile(history_for("24FJAMH3956X5ZJ"))
    verdict = agent.judge(prof, "Anand Textiles", 100, 10, [])

    assert verdict.error
    assert agent._fatal is None, "a transient failure must not stop the run"
