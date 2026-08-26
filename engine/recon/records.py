"""
The three sources, and the vocabulary for what can go wrong between them.

## Why three and not two

A two-way reconciliation - books against bank - tells a merchant that money is
missing and nothing about where it went. The gateway sits in the middle and is
the only party that knows, so leaving it out of the join is what makes the
usual answer "we are short by Rs 40,000 and nobody can say why".

    A  ERP invoices        what was BILLED
    B  gateway settlements what was PROCESSED, net of the gateway's fee
    C  bank credits        what actually ARRIVED

The interesting failures live in the seams, and they are different failures:

    A but no B   billed and never settled - the gateway has it, or it failed
    B but no C   settled and never credited - the money left the gateway and
                 did not arrive, which is the one worth chasing today
    C but no B   money arrived that nothing accounts for
    B != C       it arrived, and less of it than the gateway said it sent

## Money

Paise, as integers, everywhere. A three-way join on floating point produces
matches that are off by a hundredth of a rupee and a match rate nobody can
reproduce.

## Categories are actions, not shapes

Same rule as the settlement auditor (CLAUDE.md section 5): a finding is named
after what the merchant has to DO about it. MISSING_IN_BANK and
UNEXPLAINED_FEE look similar in a table and are completely different jobs - one
is a phone call today, the other is a line in a cost account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# --- the three sources ----------------------------------------------------


@dataclass
class Invoice:
    """Source A: what the ERP says was billed."""
    invoice_id: str
    customer_name: str
    amount: int                         # paise
    date_issued: date
    status: str = "issued"              # issued | paid | cancelled


@dataclass
class Settlement:
    """Source B: what the gateway says it processed and sent."""
    txn_id: str
    gross_amount: int                   # paise
    fee_deducted: int                   # paise, the gateway's cut plus tax
    net_settled: int                    # paise, what they say they sent
    settlement_date: date
    # Deliberately optional. A settlement line with no invoice reference is
    # the single most common real-world defect in this data, and a matcher
    # that assumes it is always present matches only the easy half.
    invoice_reference: Optional[str] = None
    utr: Optional[str] = None           # what the gateway claims it paid under


@dataclass
class BankCredit:
    """Source C: what the bank statement shows arriving."""
    utr_number: str
    description: str                    # "NEFT-RAZORPAY-SETTLEMENT-XXXX"
    credit_amount: int                  # paise
    transaction_date: date


# --- what the join can conclude -------------------------------------------

MATCHED = "MATCHED"
MATCHED_FUZZY = "MATCHED_FUZZY"
MISSING_IN_BANK = "MISSING_IN_BANK"
MISSING_IN_GATEWAY = "MISSING_IN_GATEWAY"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
UNEXPLAINED_FEE = "UNEXPLAINED_FEE"
ORPHAN_BANK_CREDIT = "ORPHAN_BANK_CREDIT"
UNRECONCILED = "UNRECONCILED"

FINDING_LABEL = {
    MATCHED: "Matched",
    MATCHED_FUZZY: "Matched without a reference",
    MISSING_IN_BANK: "Settled but never credited",
    MISSING_IN_GATEWAY: "Billed but never settled",
    AMOUNT_MISMATCH: "Bank credited a different amount",
    UNEXPLAINED_FEE: "Short by an amount nobody accounted for",
    ORPHAN_BANK_CREDIT: "Money arrived that nothing accounts for",
    UNRECONCILED: "Could not be reconciled",
}

FINDING_NOTE = {
    MATCHED:
        "All three agree: what was billed, what the gateway settled after its "
        "fee, and what the bank credited.",
    MATCHED_FUZZY:
        "Reconciled, but the gateway line carried no invoice reference - the "
        "join was made on amount and date. Worth a glance, not an alarm.",
    MISSING_IN_BANK:
        "The gateway says it sent this and the bank has no record of it. The "
        "most urgent thing this agent finds: the money left one party and did "
        "not arrive at the other, and settlement queries have a time limit.",
    MISSING_IN_GATEWAY:
        "Billed, and the gateway never settled it. Either the payment never "
        "completed or it is still in transit.",
    AMOUNT_MISMATCH:
        "The bank credited something other than what the gateway said it "
        "sent. The difference is the whole finding.",
    UNEXPLAINED_FEE:
        "Less arrived than was settled, and no fee on the statement accounts "
        "for the gap. Usually a bank charge nobody agreed to.",
    ORPHAN_BANK_CREDIT:
        "A credit with nothing behind it. Not necessarily good news - it is "
        "as likely to be somebody else's money as it is to be a windfall.",
    UNRECONCILED:
        "Nothing in the other two sources fits this. Escalated rather than "
        "forced into a category it does not belong in.",
}

# What a merchant is being asked to do. Three of these mean "no action", and
# that is deliberate: a reconciliation that flags everything is one nobody
# finishes.
ACTION_NONE = "none"
ACTION_CHASE = "chase"
ACTION_DISPUTE = "dispute"
ACTION_WRITE_OFF = "write_off"
ACTION_INVESTIGATE = "investigate"

ACTION_LABEL = {
    ACTION_NONE: "Nothing to do",
    ACTION_CHASE: "Chase the gateway today",
    ACTION_DISPUTE: "Dispute it",
    ACTION_WRITE_OFF: "Write it off",
    ACTION_INVESTIGATE: "Investigate",
}

RESOLVED = {MATCHED, MATCHED_FUZZY}


@dataclass
class ReconRow:
    """One line of the three-way join, however far it got."""
    finding: str
    invoice: Optional[Invoice] = None
    settlement: Optional[Settlement] = None
    bank: Optional[BankCredit] = None
    variance: int = 0                   # paise at stake, signed
    matched_by: str = ""                # which pass resolved it
    detail: str = ""                    # the arithmetic, in words
    # Filled by the agent, never by the matcher.
    reasoning: str = ""
    action: str = ""
    confidence: float = 0.0
    errored: bool = False

    @property
    def resolved(self) -> bool:
        return self.finding in RESOLVED

    @property
    def at_stake(self) -> int:
        return abs(self.variance)

    def as_dict(self) -> dict:
        return {
            "finding_type": self.finding,
            "finding_label": FINDING_LABEL.get(self.finding, self.finding),
            "invoice_id": self.invoice.invoice_id if self.invoice else None,
            "txn_id": self.settlement.txn_id if self.settlement else None,
            "utr_number": self.bank.utr_number if self.bank else None,
            "customer_name": self.invoice.customer_name if self.invoice else "",
            "invoice_amount": self.invoice.amount if self.invoice else 0,
            "gross_amount": self.settlement.gross_amount if self.settlement else 0,
            "fee_deducted": self.settlement.fee_deducted if self.settlement else 0,
            "net_settled": self.settlement.net_settled if self.settlement else 0,
            "credit_amount": self.bank.credit_amount if self.bank else 0,
            "variance": self.variance,
            "at_stake": self.at_stake,
            "matched_by": self.matched_by,
            "detail": self.detail,
            "reasoning": self.reasoning,
            "action": self.action,
            "action_label": ACTION_LABEL.get(self.action, self.action),
            "confidence": self.confidence,
            "resolved": self.resolved,
        }


@dataclass
class ReconBatch:
    """The three sources, ready to be joined."""
    invoices: list[Invoice] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    bank: list[BankCredit] = field(default_factory=list)

    @property
    def total_records(self) -> int:
        return len(self.invoices) + len(self.settlements) + len(self.bank)
