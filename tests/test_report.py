"""
Tests for the dashboard. Checkpoint 9.

The report is the only artefact a judge actually looks at, and it is generated
from the database rather than from the live objects - which means it can drift
from the truth in ways nothing else would catch. These tests are mostly about
that: does it reconcile, does it work offline, and can a payment id containing
a stray angle bracket break the page.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classifier import Verdict  # noqa: E402
from agent.dispute import attach_disputes  # noqa: E402
from engine.detector import detect_batch  # noqa: E402
from engine.gate import gate_batch  # noqa: E402
from engine.store import Store  # noqa: E402
from generator.synthetic import generate_batch  # noqa: E402
from report import build_html  # noqa: E402


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("report")
    b, gt = generate_batch(60)
    variances = detect_batch(b)
    verdicts = [Verdict(payment_id=v.payment_id, exception_code=gt[v.payment_id],
                        action="dispute" if gt[v.payment_id] in
                        ("ZERO_MDR_VIOLATION", "RATE_MISMATCH",
                         "INSTRUMENT_MISLABEL", "MISSING_FROM_SETTLEMENT")
                        else "fix_books",
                        confidence=0.92, reasoning="Because the rate card says so.",
                        rule_cited="rule 1 - PSS Act s.10A",
                        dispute_text="Please issue a credit note.")
                for v in variances if v.needs_agent]
    decisions = gate_batch(variances, verdicts, b.rate_card)
    disputes = attach_disputes(variances, verdicts, decisions)

    store = Store(tmp / "r.db")
    run_id = store.save_run(b, model="claude-opus-5", effort="medium")
    store.save_findings(run_id, decisions, variances, verdicts, disputes)
    html = build_html(store, run_id)
    store.close()
    return html, b, gt, variances


# --- it has to work in a room with no wifi ------------------------------

def test_the_page_loads_nothing_from_the_internet(page):
    """
    This gets opened in a venue. Every external reference is a way for the
    demo to fail in front of judges for reasons that have nothing to do with
    the product.
    """
    html, *_ = page
    assert "http://" not in html
    assert "https://" not in html
    assert "src=" not in html
    assert "<link" not in html


def test_the_page_needs_no_javascript(page):
    """
    Rows expand with <details>, which is a browser feature. Nothing here
    depends on scripting being enabled or a bundle having loaded.
    """
    html, *_ = page
    assert "<script" not in html
    assert "<details" in html
    assert "<summary" in html


# --- the money -----------------------------------------------------------

def test_the_money_panel_reconciles(page):
    """
    The first thing anyone numerate does is add up the column. An earlier
    version showed gross, fees and GST against a bank credit Rs 61,952 away
    from their difference, because refunds, an unsettled payment and an
    adjustment were all missing.
    """
    html, *_ = page
    assert "Every line reconciles to the paise" in html
    assert "do not trust this panel" not in html


def test_the_panel_names_every_deduction(page):
    html, *_ = page
    for label in ("Gross sales", "Refunded to customers", "Gateway fees",
                  "GST on fees", "Credited to the bank"):
        assert label in html


def test_no_money_is_rendered_as_a_bare_float(page):
    """Paise in, formatted rupees out. A stray 1626.9999 must never surface."""
    html, *_ = page
    assert not re.search(r"\d+\.\d{3,}", html)


# --- the findings --------------------------------------------------------

def test_every_finding_appears(page):
    html, _, gt, _ = page
    for payment_id in gt:
        assert payment_id in html


def test_the_reasoning_and_the_rule_are_both_shown(page):
    """
    Guardrail 2: a classification carries a reasoning trace AND the rule it
    relied on. A dashboard that shows only the verdict has dropped half of it.
    """
    html, *_ = page
    assert "Because the rate card says so." in html
    assert "Rule relied on:" in html
    assert "PSS Act s.10A" in html


def test_dispute_messages_are_shown_where_they_exist(page):
    html, *_ = page
    assert "Ready to send" in html
    assert "--- Reference details ---" in html


def test_the_human_queue_says_why(page):
    html, *_ = page
    assert "Held for a human:" in html
    assert "review threshold" in html


def test_the_accuracy_panel_reports_the_measured_number(page):
    html, *_ = page
    assert "Measured against a known answer key" in html
    assert "planted anomalies caught" in html
    assert "false accusations" in html


def test_the_answer_key_is_not_embedded_per_record(page):
    """
    The page shows what the SYSTEM concluded and an aggregate score. It must
    not carry the per-record answer, or a reader cannot tell the difference
    between a result and a lookup.
    """
    html, *_ = page
    for word in ("ground_truth", "planted_code", "answer_key", "truth_for"):
        assert word not in html


# --- robustness ----------------------------------------------------------

def test_html_is_escaped(tmp_path):
    """
    Ids come from data. A payment id is not a place to trust, and an unescaped
    one turns a report into a broken page.
    """
    b, gt = generate_batch(60)
    b.records[0].payment.payment_id = "<script>alert(1)</script>"
    b.records[0].record_id = "<script>alert(1)</script>"
    variances = detect_batch(b)
    decisions = gate_batch(variances, [], b.rate_card)

    store = Store(tmp_path / "x.db")
    run_id = store.save_run(b)
    store.save_findings(run_id, decisions, variances, [])
    html = build_html(store, run_id)
    store.close()

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_run_with_no_agent_decisions_still_renders(tmp_path):
    """A calculator-only run is a legitimate run, not an error state."""
    b, gt = generate_batch(60)
    variances = detect_batch(b)
    decisions = gate_batch(variances, [], b.rate_card)

    store = Store(tmp_path / "empty.db")
    run_id = store.save_run(b)
    store.save_findings(run_id, decisions, variances, [])
    html = build_html(store, run_id)
    store.close()
    assert "Settlement deduction audit" in html
    assert len(html) > 5000


def test_the_scorecard_is_skipped_when_it_cannot_be_trusted(tmp_path):
    """
    Ground truth is rebuilt from the stored seed. If the findings do not match
    what that seed produces, the run did not come from this generator and
    scoring it would be inventing a number.
    """
    from report import _scorecard

    b, gt = generate_batch(60)
    variances = detect_batch(b)
    decisions = gate_batch(variances, [], b.rate_card)

    store = Store(tmp_path / "mismatch.db")
    run_id = store.save_run(b)
    store.save_findings(run_id, decisions, variances, [])
    store.conn.execute("UPDATE runs SET seed = 999999 WHERE run_id = ?", (run_id,))
    store.conn.commit()

    assert _scorecard(store, run_id) is None
    assert "Measured against a known answer key" not in build_html(store, run_id)
    store.close()


# --- one design system, two artefacts ------------------------------------

def test_the_report_uses_the_same_design_tokens_as_the_app(page):
    """
    The report used to carry its own palette. It drifted the moment the app was
    restyled, and left two artefacts of the same product looking like different
    products. Both now read the same tokens, so they cannot drift again.
    """
    from merchant.views import COMPONENTS, TOKENS

    html, *_ = page
    assert TOKENS in html
    assert COMPONENTS in html


def test_no_trace_of_the_old_palette_remains(page):
    html, *_ = page
    for stale in ("#fbfaf9", "--dispute", "--fix:", "--escalate", "--dismiss",
                  'class="panel"', 'class="pid"', 'class="code"'):
        assert stale not in html, stale


def test_colour_follows_the_action_not_the_code(page):
    """
    CLAUDE.md section 5: the taxonomy is organised by what the merchant must DO.
    Two findings with different codes and the same required action should look
    the same, because to the merchant they are the same.
    """
    import re

    html, *_ = page
    pairs = dict(re.findall(r'class="pill (\w*)">\s*([A-Z_]+)', html)[::-1])
    by_code = {code: cls for cls, code in
               re.findall(r'class="pill (\w*)">\s*([A-Z_]+)', html)}

    # both recoverable dispute codes carry the same class
    assert by_code.get("ZERO_MDR_VIOLATION") == by_code.get("INSTRUMENT_MISLABEL")
    assert by_code.get("ZERO_MDR_VIOLATION") == "danger"
    # a books problem is not a dispute
    assert by_code.get("GST_MISMATCH") == "warn"
    # and the do-nothing codes are quiet
    assert by_code.get("CLEAN", "") == ""


def test_the_report_follows_the_same_light_palette(page):
    """
    It shares TOKENS, so committing the app to light committed the report too -
    which is the shared design system doing its job.
    """
    html, *_ = page
    assert "prefers-color-scheme" not in html
    assert "color-scheme: light" in html


def test_the_report_still_needs_nothing_from_the_network(page):
    """Sharing a stylesheet must not have introduced a link to one."""
    html, *_ = page
    assert "http://" not in html and "https://" not in html
    assert "<link" not in html and "<script" not in html
