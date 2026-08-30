"""
The vocabulary for the outward-tax controller. Four small enums, one per
layer, because each layer answers a different question and conflating them
into one taxonomy would blur what a merchant has to DO (CLAUDE.md section 5)
- classifying a sale, timing a correction, allocating cash, and choosing a
QRMP method are four different jobs, not four flavours of one job.

## Why layers 1 and 4 have no judgment codes

Nothing here is ambiguous at the record level for either. Layer 1
(classifier.py) is a pure rule given amount + buyer-GSTIN presence; layer 4
(qrmp.py) is a pure comparison between two computed rupee figures. See each
module's own docstring for the specific reasoning - the same discipline
already applied to engine/payout_timing/taxonomy.py, where "nothing here
needs a per-record UNEXPLAINED because nothing here is genuinely ambiguous."

Layers 2 and 3 are where real judgment lives - see agent/gst_correction_classifier.py
and agent/gst_filing_documents.py.
"""

from __future__ import annotations

from enum import StrEnum


# --- layer 1: classification -------------------------------------------

class InvoiceType(StrEnum):
    B2B = "b2b"      # registered buyer
    B2CL = "b2cl"    # unregistered, interstate, above the value threshold
    B2CS = "b2cs"    # unregistered, everything else


class GSTR1Code(StrEnum):
    CLASSIFIED = "CLASSIFIED"                        # clean, ready to assemble
    IRN_MISSING = "IRN_MISSING"                       # B2B, e-invoicing applies, no IRN
    HSN_RATE_UNCONFIGURED = "HSN_RATE_UNCONFIGURED"   # can't compute tax - excluded, not guessed


GSTR1_LABEL: dict[GSTR1Code, str] = {
    GSTR1Code.CLASSIFIED: "Classified",
    GSTR1Code.IRN_MISSING: "Missing an e-invoice IRN",
    GSTR1Code.HSN_RATE_UNCONFIGURED: "HSN has no rate on file",
}


# --- layer 2: the GSTR-1A / lock timing state machine -------------------

class WindowState(StrEnum):
    OPEN = "open"        # GSTR-3B not yet filed - GSTR-1A still free
    LOCKED = "locked"    # GSTR-3B filed - liability table hard-locked since Jul 2025


class CorrectionCode(StrEnum):
    PERIOD_CLEAN = "PERIOD_CLEAN"                # GSTR-1 and GSTR-3B agree
    CORRECTABLE_VIA_1A = "CORRECTABLE_VIA_1A"     # mismatch, window still open
    LOCKED_NEEDS_DRC03 = "LOCKED_NEEDS_DRC03"     # mismatch, window closed


CORRECTION_LABEL: dict[CorrectionCode, str] = {
    CorrectionCode.PERIOD_CLEAN: "GSTR-1 and GSTR-3B agree",
    CorrectionCode.CORRECTABLE_VIA_1A: "Still correctable via GSTR-1A",
    CorrectionCode.LOCKED_NEEDS_DRC03: "Locked - needs a DRC-03 payment",
}

# What a merchant does about each - three of four codes mean "watch/act", one
# means "nothing", the same three-vs-rest split every taxonomy in this
# project keeps deliberately visible.
class CorrectionAction(StrEnum):
    NONE = "none"
    FILE_1A = "file_1a"
    PAY_DRC03 = "pay_drc03"


CORRECTION_ACTION_FOR: dict[CorrectionCode, CorrectionAction] = {
    CorrectionCode.PERIOD_CLEAN: CorrectionAction.NONE,
    CorrectionCode.CORRECTABLE_VIA_1A: CorrectionAction.FILE_1A,
    CorrectionCode.LOCKED_NEEDS_DRC03: CorrectionAction.PAY_DRC03,
}


# --- layer 3: the offset / Rule 88C shield -------------------------------

class OffsetCode(StrEnum):
    OFFSET_CLEAN = "OFFSET_CLEAN"
    RULE_88C_BREACH = "RULE_88C_BREACH"


OFFSET_LABEL: dict[OffsetCode, str] = {
    OffsetCode.OFFSET_CLEAN: "Within Rule 88C",
    OffsetCode.RULE_88C_BREACH: "Rule 88C notice risk",
}


# --- layer 4: QRMP --------------------------------------------------------

class QRMPMethod(StrEnum):
    FIXED_SUM = "fixed_sum"
    SELF_ASSESSMENT = "self_assessment"
