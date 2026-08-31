"""
Synthetic purchase line items, with known overbilling and known
unconfigured items planted. Same trick as every generator in this project
(CLAUDE.md section 7): plant the answer, hand back the key, and the demo
becomes a measurement.

The demo rate card is returned alongside the line items rather than baked
into engine/vendor_terms/detector.py, the same separation
engine/gst_filing/generator.py keeps between DEMO_RATE_CARD and the HSN
classifier - the engine takes a rate card as an argument regardless of
where it came from, generated here or entered by a merchant.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from engine.vendor_terms.detector import LineItem
from engine.vendor_terms.rules import normalise_item_key
from engine.vendor_terms.taxonomy import TermsCode

AS_OF = date(2026, 8, 24)

# A small, honest demo catalogue - not a real price list, just plausible
# line items at a plausible contracted price in paise. One item
# ("Packing Tape - 2 inch") is deliberately never given a rate-card entry
# - the RATE_UNCONFIGURED plant, same role UNCONFIGURED_HSN plays in
# engine/gst_filing/generator.py.
ITEM_CATALOGUE: list[tuple[str, int]] = [
    ("Steel Rod - 12mm", 68_00),
    ("Cement - OPC 53 Grade (bag)", 412_00),
    ("Corrugated Box - Large", 34_50),
    ("Cotton Fabric - per metre", 145_00),
    ("Printer Cartridge - Black", 890_00),
    ("Plywood Sheet - 19mm", 2_150_00),
    ("Copper Wire - 2.5mm (coil)", 1_680_00),
]
UNCONFIGURED_ITEM = "Packing Tape - 2 inch"

SUPPLIERS = ["Anand Steel Traders", "Konkan Cement Supply",
            "Bright Packaging Co", "Surat Textile Mills",
            "Nashik Office Systems", "Deccan Timber Depot"]

CANONICAL_MIX: dict[str, int] = {
    "clean": 32,
    "overbilled": 4,
    "unconfigured": 4,
}


def _gstin(rng: random.Random) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    state = rng.choice(["27", "24", "29", "07", "33"])
    pan = ("".join(rng.choice(letters) for _ in range(5))
          + f"{rng.randint(0, 9999):04d}" + rng.choice(letters))
    return f"{state}{pan}{rng.randint(1, 9)}Z{rng.choice(letters)}"


def generate_line_items(n: int = 40, seed: int = 20260827
                        ) -> tuple[list[LineItem], dict[str, str],
                                  dict[tuple[str, str], int]]:
    """
    Returns (items, ground_truth, rate_card):
      ground_truth maps line_item_id -> the TermsCode it was built to produce
      rate_card maps (supplier_gstin, item_key) -> contracted unit price,
        the same shape engine.vendor_terms.detector.detect() takes
    """
    rng = random.Random(seed)
    recipes = _recipe_list(n, rng)
    start = date(2026, 8, 1)

    supplier_gstins = {name: _gstin(rng) for name in SUPPLIERS}
    rate_card: dict[tuple[str, str], int] = {}
    for name in SUPPLIERS:
        gstin = supplier_gstins[name]
        for description, price in ITEM_CATALOGUE:
            rate_card[(gstin, normalise_item_key(description))] = price

    items: list[LineItem] = []
    truth: dict[str, str] = {}

    for i, recipe in enumerate(recipes, start=1):
        line_item_id = f"LI-{i:04d}"
        purchase_id = f"pur_demo_{(i - 1) // 3 + 1:03d}"
        supplier = SUPPLIERS[i % len(SUPPLIERS)]
        gstin = supplier_gstins[supplier]
        issued = start + timedelta(days=rng.randint(0, 20))
        quantity_x100 = rng.randint(2, 40) * 100

        if recipe == "unconfigured":
            description = UNCONFIGURED_ITEM
            unit_price = rng.randint(1_500, 6_000)
            truth[line_item_id] = str(TermsCode.RATE_UNCONFIGURED)
        else:
            description, contracted = rng.choice(ITEM_CATALOGUE)
            if recipe == "overbilled":
                # Findable: 8-20% over, well past the 0.5% tolerance band.
                markup = rng.randint(800, 2_000)          # bps
                unit_price = contracted + (contracted * markup) // 10_000
                truth[line_item_id] = str(TermsCode.OVERBILLED)
            else:                                          # clean
                # Either exact, or a small drift a real accounts system
                # would produce (rounding, a paise of currency conversion)
                # - always inside the tolerance band.
                drift = rng.randint(-40, 40)
                unit_price = max(1, contracted + drift)
                truth[line_item_id] = str(TermsCode.RATE_CLEAN)

        items.append(LineItem(
            line_item_id=line_item_id, purchase_id=purchase_id,
            supplier_name=supplier, supplier_gstin=gstin,
            invoice_number=f"INV/{purchase_id[-3:]}/{i:03d}",
            invoice_date=issued, description=description,
            item_key=normalise_item_key(description),
            quantity_x100=quantity_x100, unit_price_paise=unit_price,
            line_total_paise=(unit_price * quantity_x100) // 100))

    return items, truth, rate_card


def _recipe_list(n: int, rng: random.Random) -> list[str]:
    if n == 40:
        recipes = [r for r, count in CANONICAL_MIX.items() for _ in range(count)]
    else:
        recipes = []
        for recipe, count in CANONICAL_MIX.items():
            recipes += [recipe] * max(1, round(count * n / 40))
        recipes = recipes[:n]
    rng.shuffle(recipes)
    return recipes
