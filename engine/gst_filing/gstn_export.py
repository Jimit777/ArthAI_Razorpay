"""
The real GSTN offline-utility JSON shape for GSTR-1 and GSTR-1A, and the
real e-invoice IRN-generation request shape - not this project's own shape
any more.

## What "verified" means here, precisely

Every field name and the nesting below is cross-checked against two
independent, real sources, read directly this session:

  1. A certified GST Suvidha Provider's own developer API documentation
     for the GSTR-1 filing endpoint (Sandbox.co.in).
  2. resilient-tech/india-compliance, an actively-maintained, open-source
     GST compliance app used in production by real Indian businesses on
     Frappe/ERPNext - its GSTR-1 section builders (b2b.py, b2cl.py,
     b2cs.py, hsn.py, doc_issue.py, fields/gstr1.py) and its e-invoice
     test fixture (test_e_invoice.json, a real recorded request/response
     pair against NIC's IRP, including a real signed IRN response) were
     read in full.

Both agree on every field below (gstin, fp, ctin, inv, itms, itm_det, rt,
txval, iamt, camt, samt, csamt, sply_ty, hsn_sc, doc_det - and the
e-invoice Version/TranDtls/DocDtls/SellerDtls/BuyerDtls/ItemList/ValDtls
set) - the strongest confirmation available short of a live GSTN/NIC
login, since nobody outside GSTN can test an actual portal upload from
here.

## What is still NOT verified, and stays disclaimed

Nobody has tested this JSON against an actual GSTN portal upload or a
live NIC IRP submission - that needs a real, live-filing-window GSTIN,
which cannot exist in a demo. "Matches the documented schema" and "has
been accepted by the portal" are different claims; only the first is
made here. GSTR-1A's amendment-specific fields (whether the portal wants
an explicit "original value" alongside the revised one) were not
separately confirmed beyond the base section shape - see to_gstr1a_json.

## Why the e-invoice payload still has holes, and they are shown, not filled

SellerDtls and BuyerDtls each need a full postal address (Addr1, Loc,
Pin, Stcd). The merchant's own address is now a one-time setting (see
Ledger.set_gst_profile) - but a buyer's address has never been part of
any sale record this system collects, and manufacturing one would be
inventing data about a real counterparty, the exact failure CLAUDE.md
section 16 warns against. Missing fields are named in a per-invoice
`missing_fields` list, never silently defaulted - the same "absence is
not innocence" discipline classifier.py already applies to an
unconfigured HSN rate.

## Why GSTR-1A is expressed as a B2CS-style aggregate amendment

Layer 2's own correction detection (engine.gst_filing.timing) works at
the PERIOD level - one scalar gap between what GSTR-1 supports and what
GSTR-3B is about to pay, not an attribution to any specific invoice. The
real B2CS table is itself state+rate aggregate with no invoice-level
detail at all, which happens to be exactly the right shape for a
correction this system only knows in aggregate - so the amendment is
built as a b2csa-style entry, not a b2b/b2cl per-invoice amendment. This
is not a claim of knowing which invoice(s) caused the gap.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from engine.gst_filing.classifier import ClassifiedInvoice, GSTR1Draft


def _gov_date(d: date) -> str:
    """Portal writes GSTR-1 dates DD-MM-YYYY."""
    return d.strftime("%d-%m-%Y")


def _einvoice_date(d: date) -> str:
    """The e-invoice schema writes dates DD/MM/YYYY - a different
    separator from GSTR-1's own DD-MM-YYYY, confirmed from the real
    request/response fixture read this session."""
    return d.strftime("%d/%m/%Y")


def _rupees(paise: int) -> float:
    return round((paise or 0) / 100, 2)


def _filing_period(period: str) -> str:
    """'2026-08' -> '082026' - the portal's MMYYYY, not our own YYYY-MM."""
    year, month = period.split("-")
    return f"{month}{year}"


def _rate_pct(inv: ClassifiedInvoice) -> float:
    """Recovered from the tax actually computed, not stored separately -
    classify() never keeps the rate_bps it looked up once tax is split."""
    if not inv.taxable_value:
        return 0.0
    return round((inv.total_tax / inv.taxable_value) * 100, 2)


def _item_row(inv: ClassifiedInvoice, *, igst_only: bool = False) -> dict:
    itm_det = {"rt": _rate_pct(inv), "txval": _rupees(inv.taxable_value),
              "iamt": _rupees(inv.igst)}
    if not igst_only:
        itm_det["camt"] = _rupees(inv.cgst)
        itm_det["samt"] = _rupees(inv.sgst)
    itm_det["csamt"] = 0
    return {"num": 1, "itm_det": itm_det}


def to_gstr1_json(draft: GSTR1Draft, *, gstin: str, home_state: str) -> dict:
    """The real GSTN offline-utility JSON shape - see this module's
    docstring for what "real" means here."""
    b2b_by_buyer: dict[str, list] = {}
    for inv in draft.b2b:
        b2b_by_buyer.setdefault(inv.buyer_gstin or "", []).append(inv)

    b2b = [{
        "ctin": buyer_gstin,
        "inv": [{
            "inum": inv.invoice_number, "idt": _gov_date(inv.invoice_date),
            "val": _rupees(inv.taxable_value + inv.total_tax),
            "pos": inv.place_of_supply, "rchrg": "N",
            "inv_typ": "R", "itms": [_item_row(inv)],
        } for inv in invs],
    } for buyer_gstin, invs in b2b_by_buyer.items()]

    b2cl_by_pos: dict[str, list] = {}
    for inv in draft.b2cl:
        b2cl_by_pos.setdefault(inv.place_of_supply, []).append(inv)

    b2cl = [{
        "pos": pos,
        "inv": [{
            "inum": inv.invoice_number, "idt": _gov_date(inv.invoice_date),
            "val": _rupees(inv.taxable_value + inv.total_tax),
            "itms": [_item_row(inv, igst_only=True)],
        } for inv in invs],
    } for pos, invs in b2cl_by_pos.items()]

    b2cs: dict[tuple, dict] = {}
    for inv in draft.b2cs:
        key = (inv.place_of_supply, _rate_pct(inv))
        row = b2cs.setdefault(key, {
            "sply_ty": "INTER" if inv.place_of_supply != home_state else "INTRA",
            "pos": inv.place_of_supply, "typ": "OE", "rt": key[1],
            "txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0,
        })
        row["txval"] += _rupees(inv.taxable_value)
        row["iamt"] += _rupees(inv.igst)
        row["camt"] += _rupees(inv.cgst)
        row["samt"] += _rupees(inv.sgst)

    hsn_data = [{
        "num": i + 1, "hsn_sc": h["hsn_code"], "desc": "",
        "uqc": "OTH-OTHERS", "qty": 0,
        "txval": _rupees(h["taxable_value"]), "iamt": _rupees(h["igst"]),
        "camt": _rupees(h["cgst"]), "samt": _rupees(h["sgst"]), "csamt": 0,
        "rt": 0,
    } for i, h in enumerate(draft.hsn_summary)]

    all_numbers = sorted(
        inv.invoice_number for inv in draft.b2b + draft.b2cl + draft.b2cs)
    doc_issue = {}
    if all_numbers:
        doc_issue = {"doc_det": [{
            "doc_num": 1,
            "docs": [{"num": 1, "from": all_numbers[0], "to": all_numbers[-1],
                     "totnum": len(all_numbers), "cancel": 0,
                     "net_issue": len(all_numbers)}],
        }]}

    gt = _rupees(draft.total_taxable + draft.total_tax)
    return {
        "gstin": gstin, "fp": _filing_period(draft.period),
        "gt": gt, "cur_gt": gt,
        "b2b": b2b, "b2cl": b2cl, "b2cs": list(b2cs.values()),
        "hsn": {"data": hsn_data}, "doc_issue": doc_issue,
    }


def to_gstr1a_json(g1a_draft: dict, *, gstin: str, period: str,
                   home_state: str) -> dict:
    """A b2csa-style aggregate amendment - see this module's docstring for
    why the aggregate shape, not a per-invoice one."""
    return {
        "gstin": gstin, "fp": _filing_period(period),
        "b2csa": [{
            "sply_ty": "INTRA", "pos": home_state, "typ": "OE", "rt": 0,
            "txval": _rupees(g1a_draft["amendment_paise"]),
            "iamt": 0, "camt": 0, "samt": 0, "csamt": 0,
        }],
    }


def to_einvoice_request(inv: ClassifiedInvoice, *, seller: dict,
                        home_state: str) -> dict:
    """One INV-01 IRN-generation request. `missing_fields` names every
    address field this system does not have data for - never guessed,
    never silently dropped."""
    missing_fields: list[str] = []

    seller_dtls = {
        "Gstin": seller.get("gstin", ""),
        "LglNm": seller.get("legal_name", ""),
        "TrdNm": seller.get("trade_name") or seller.get("legal_name", ""),
        "Addr1": seller.get("address_line1", ""),
        "Loc": seller.get("location", ""),
        "Pin": seller.get("pincode", ""),
        "Stcd": home_state,
    }
    for key, label in (("gstin", "seller GSTIN"), ("legal_name", "seller legal name"),
                       ("address_line1", "seller address"),
                       ("location", "seller location"),
                       ("pincode", "seller PIN code")):
        if not seller.get(key):
            missing_fields.append(label)

    buyer_dtls = {
        "Gstin": inv.buyer_gstin or "", "LglNm": inv.buyer_name,
        "TrdNm": inv.buyer_name, "Pos": inv.place_of_supply,
        "Stcd": inv.place_of_supply,
        "Addr1": "", "Loc": "", "Pin": "",
    }
    missing_fields += ["buyer address", "buyer location", "buyer PIN code"]

    rate_pct = _rate_pct(inv)
    taxable = _rupees(inv.taxable_value)
    total = _rupees(inv.taxable_value + inv.total_tax)
    item = {
        "SlNo": "1", "IsServc": "N", "PrdDesc": f"HSN {inv.hsn_code}",
        "HsnCd": inv.hsn_code, "Qty": 1, "Unit": "OTH",
        "UnitPrice": taxable, "TotAmt": taxable, "Discount": 0,
        "AssAmt": taxable, "GstRt": rate_pct,
        "IgstAmt": _rupees(inv.igst), "CgstAmt": _rupees(inv.cgst),
        "SgstAmt": _rupees(inv.sgst), "CesRt": 0, "CesAmt": 0,
        "CesNonAdvlAmt": 0, "OthChrg": 0, "TotItemVal": total,
    }

    return {
        "Version": "1.1",
        "TranDtls": {"TaxSch": "GST", "SupTyp": "B2B", "RegRev": "N"},
        "DocDtls": {"Typ": "INV", "No": inv.invoice_number,
                   "Dt": _einvoice_date(inv.invoice_date)},
        "SellerDtls": seller_dtls, "BuyerDtls": buyer_dtls,
        "ItemList": [item],
        "ValDtls": {
            "AssVal": taxable, "CgstVal": _rupees(inv.cgst),
            "SgstVal": _rupees(inv.sgst), "IgstVal": _rupees(inv.igst),
            "CesVal": 0, "Discount": 0, "OthChrg": 0, "RndOffAmt": 0,
            "TotInvVal": total,
        },
        "missing_fields": missing_fields,
    }


def to_einvoice_batch(invoices: list[ClassifiedInvoice], *, seller: dict,
                      home_state: str) -> list[dict]:
    return [to_einvoice_request(inv, seller=seller, home_state=home_state)
            for inv in invoices]
