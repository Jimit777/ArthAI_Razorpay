"""
Businesses, and the contract each one signed.

The platform is multi-tenant from here on. That is not a scale decision - there
is one operator and a demo - it is a CORRECTNESS decision, and it took a while
to see why.

## Why the rate card has to belong to the business

"Charged above the contracted slab" is the second most common finding this
system produces, and it is meaningless without a contract to compare against.
Until now the rate card was one JSON file shared by everyone, which quietly
assumed every merchant in India negotiated the same rates. They do not - that
is what "negotiated" means. A boutique doing Rs 4 lakh a month and a chain doing
Rs 4 crore are on different credit-card slabs, and an auditor that cannot model
that cannot audit either of them.

So each business gets its own rate card, seeded from the reference file and
editable. The RBI caps stay fixed because they are law; the negotiated slabs
move because they are negotiated.

## What is deliberately absent

Passwords. CLAUDE.md section 16 says not to build auth and that is the right
call for the time available - but an unauthenticated multi-tenant app should say
so rather than imply a security boundary that does not exist. The UI says it.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from engine.expected_value import RATE_CARD_PATH

# Rates set by regulation rather than by negotiation. A merchant cannot agree to
# a higher one, so the editor will not let them try.
REGULATED = {
    "upi": "PSS Act s.10A (as amended 2026)",
    "rupay_debit": "PSS Act s.10A (as amended 2026)",
    "debit_card_low": "RBI circular RBI/2017-18/105",
    "debit_card_high": "RBI circular RBI/2017-18/105",
}

BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
  business_id        TEXT PRIMARY KEY,
  name               TEXT NOT NULL,
  slug               TEXT UNIQUE,
  gateway_behaviour  TEXT DEFAULT 'correct',
  gst_rate_bps       INTEGER DEFAULT 1800,
  tolerance_floor_paise INTEGER DEFAULT 100,
  tolerance_pct_bps  INTEGER DEFAULT 50,
  -- What the system may close by itself. Per business on purpose: a Rs 250
  -- review threshold means one thing to a boutique and nothing at all to a
  -- chain doing Rs 4 crore a month.
  min_confidence     REAL DEFAULT 0.75,
  review_above_paise INTEGER DEFAULT 25000,
  created_at         INTEGER
);

-- One row per instrument per business: the contract they actually signed.
CREATE TABLE IF NOT EXISTS business_rate_card (
  business_id         TEXT,
  instrument          TEXT,
  label               TEXT,
  network_mdr_bps     INTEGER,
  network_mdr_cap_bps INTEGER,
  network_mdr_source  TEXT,
  platform_fee_bps    INTEGER,
  platform_fee_source TEXT,
  PRIMARY KEY (business_id, instrument)
);

-- Which agents a business has turned on. Only one is live today; the table
-- exists so the second one is a row rather than a migration.
CREATE TABLE IF NOT EXISTS business_agents (
  business_id TEXT,
  agent_id    TEXT,
  enabled     INTEGER DEFAULT 1,
  enabled_at  INTEGER,
  PRIMARY KEY (business_id, agent_id)
);
"""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "business"


def new_business_id() -> str:
    return f"biz_{secrets.token_hex(6)}"


def reference_rate_card() -> dict:
    """The template every new business starts from."""
    with open(RATE_CARD_PATH) as f:
        return json.load(f)


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    """
    Add a column to an existing table, once.

    Every table here is CREATE TABLE IF NOT EXISTS, which silently does nothing
    when the table already exists - so a new column never reaches a database
    that predates it. This is the smallest thing that fixes that without
    pulling in a migration framework.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


class Businesses:
    """Business records and their contracts. Wraps an open connection."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(BUSINESS_SCHEMA)
        _add_column(conn, "businesses", "archived_at", "INTEGER")
        _add_column(conn, "businesses", "supplier_behaviour",
                    "TEXT DEFAULT 'correct'")
        self.conn.commit()

    # --- creating ---------------------------------------------------------

    def create(self, name: str, template: Optional[dict] = None) -> str:
        name = name.strip()
        if not name:
            raise ValueError("a business needs a name")

        reference = template or reference_rate_card()
        business_id = new_business_id()

        slug = base = slugify(name)
        n = 2
        while self.conn.execute("SELECT 1 FROM businesses WHERE slug = ?",
                                (slug,)).fetchone():
            slug, n = f"{base}-{n}", n + 1

        guardrails = reference.get("guardrails", {})
        self.conn.execute(
            "INSERT INTO businesses (business_id, name, slug, gateway_behaviour,"
            " gst_rate_bps, tolerance_floor_paise, tolerance_pct_bps,"
            " min_confidence, review_above_paise, created_at)"
            " VALUES (?,?,?,'correct',?,?,?,?,?,?)",
            (business_id, name, slug, reference["gst_rate_bps"],
             reference["tolerance"]["floor_paise"], reference["tolerance"]["pct_bps"],
             guardrails.get("min_confidence", 0.75),
             guardrails.get("review_above_paise", 25_000),
             int(time.time())))

        for key, spec in reference["instruments"].items():
            self.conn.execute(
                "INSERT INTO business_rate_card (business_id, instrument, label,"
                " network_mdr_bps, network_mdr_cap_bps, network_mdr_source,"
                " platform_fee_bps, platform_fee_source) VALUES (?,?,?,?,?,?,?,?)",
                (business_id, key, spec["label"], spec["network_mdr_bps"],
                 spec.get("network_mdr_cap_bps"), spec["network_mdr_source"],
                 spec["platform_fee_bps"], spec["platform_fee_source"]))

        # Turn the live agents on explicitly rather than treating "no rows" as
        # "everything on". That implicit convention broke the moment a row
        # existed with enabled=0: the set came back empty again and read as
        # enabled. A default you have to infer is a bug waiting for its second
        # state.
        import merchant.agents.settlement  # noqa: F401  - registers the agent
        from merchant.catalog import live_agents

        for spec in live_agents():
            self.conn.execute(
                "INSERT INTO business_agents (business_id, agent_id, enabled,"
                " enabled_at) VALUES (?,?,1,?)",
                (business_id, spec.id, int(time.time())))

        self.conn.commit()
        return business_id

    # --- reading ----------------------------------------------------------

    def all(self, include_archived: bool = False) -> list:
        where = "" if include_archived else " WHERE b.archived_at IS NULL"
        return self.conn.execute(
            "SELECT b.*, ("
            "  SELECT COUNT(*) FROM live_payments p WHERE p.business_id = b.business_id"
            f" ) AS payments FROM businesses b{where} ORDER BY created_at").fetchall()

    def get(self, business_id: str):
        return self.conn.execute(
            "SELECT * FROM businesses WHERE business_id = ?",
            (business_id,)).fetchone()

    def by_slug(self, slug: str):
        return self.conn.execute(
            "SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()

    # --- putting one away, or removing it ---------------------------------
    #
    # Two different things, and which one you get is not a preference:
    #
    #   never audited -> delete. Nothing was ever concluded about anyone's
    #                    money, so there is no finding and no agent decision to
    #                    destroy. It is a name and a default rate card.
    #   ever audited  -> archive only. Its settlements carry findings, the
    #                    reasoning behind them, and the log of what the agent
    #                    decided. Guardrail 5 says every agent decision is
    #                    replayable; a delete button that erases them on request
    #                    makes that promise conditional, which is the same as
    #                    not making it.

    def settlement_count(self, business_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM business_runs WHERE business_id = ?",
            (business_id,)).fetchone()["n"]

    def may_delete(self, business_id: str) -> bool:
        """Deletable only while nothing has ever been concluded about its money."""
        return self.settlement_count(business_id) == 0

    def archive(self, business_id: str) -> None:
        self.conn.execute(
            "UPDATE businesses SET archived_at = ? WHERE business_id = ?",
            (int(time.time()), business_id))
        self.conn.commit()

    def restore(self, business_id: str) -> None:
        self.conn.execute(
            "UPDATE businesses SET archived_at = NULL WHERE business_id = ?",
            (business_id,))
        self.conn.commit()

    def is_archived(self, business_id: str) -> bool:
        row = self.get(business_id)
        return bool(row and row["archived_at"])

    # Every table that holds something belonging to one business. Listed here
    # rather than discovered, so adding a table is a deliberate decision about
    # what deleting a business should take with it.
    OWNED_TABLES = ("business_runs", "live_orders", "live_payments",
                    "memberships", "business_rate_card", "business_agents",
                    "data_sources", "access_log")

    def delete(self, business_id: str) -> dict:
        """
        Remove a business and everything belonging to it.

        Refuses outright if it has ever been audited - see may_delete. The
        caller is expected to have checked and offered archiving instead; this
        check exists so that a future caller that forgets cannot quietly
        destroy an audit trail.
        """
        if not self.may_delete(business_id):
            raise ValueError(
                "this business has settlements, so it can be archived but not "
                "deleted")

        # Each of these tables is created by whichever module owns it, on
        # first use - so on a young database some of them do not exist yet.
        # A business created and deleted before anything touched the access
        # log is an ordinary case, not an error.
        present = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}

        removed = {}
        for table in self.OWNED_TABLES:
            if table not in present:
                continue
            n = self.conn.execute(
                f"DELETE FROM {table} WHERE business_id = ?",
                (business_id,)).rowcount
            if n:
                removed[table] = n
        n = self.conn.execute("DELETE FROM businesses WHERE business_id = ?",
                              (business_id,)).rowcount
        if n:
            removed["businesses"] = n
        self.conn.commit()
        return removed

    def rate_card(self, business_id: str) -> dict:
        """
        Rebuild the business's contract in the exact shape the engine expects.

        The engine already takes a rate card as an argument, so a per-business
        contract is a drop-in - nothing in engine/ changed to support this.
        """
        row = self.get(business_id)
        if row is None:
            raise KeyError(business_id)

        reference = reference_rate_card()

        instruments = {}
        for spec in self.conn.execute(
                "SELECT * FROM business_rate_card WHERE business_id = ?",
                (business_id,)):
            key = spec["instrument"]

            # A regulated citation is not the merchant's to keep a copy of.
            # They cannot negotiate the rate, so the authority for it belongs
            # to the law and is read from the reference card every time rather
            # than from the row copied in when the business was created.
            #
            # Without this, the 17 Aug 2026 change to the MDR authority would
            # have needed a hand-edit for every business on the platform, and
            # any business created before that date would have gone on citing
            # a link that no longer exists. Rule provenance that silently goes
            # stale is worse than no provenance: it is a wrong citation stated
            # with confidence.
            source = spec["network_mdr_source"]
            if key in REGULATED and key in reference["instruments"]:
                source = reference["instruments"][key]["network_mdr_source"]

            instruments[key] = {
                "label": spec["label"],
                "network_mdr_bps": spec["network_mdr_bps"],
                "network_mdr_cap_bps": spec["network_mdr_cap_bps"],
                "network_mdr_source": source,
                "platform_fee_bps": spec["platform_fee_bps"],
                "platform_fee_source": spec["platform_fee_source"],
            }
        return {
            "merchant_id": business_id,
            "currency": "INR",
            "gst_rate_bps": row["gst_rate_bps"],
            "gst_source": reference["gst_source"],
            "tolerance": {"floor_paise": row["tolerance_floor_paise"],
                          "pct_bps": row["tolerance_pct_bps"]},
            # engine/gate.py reads these. Omitting them made every audit die
            # with KeyError('guardrails') - the gate has no defaults on purpose,
            # because silently guessing what may be auto-closed is exactly the
            # decision that should never be implicit.
            "guardrails": {
                "min_confidence": row["min_confidence"],
                "review_above_paise": row["review_above_paise"],
            },
            "instruments": instruments,
        }

    # --- editing ----------------------------------------------------------

    def set_rate(self, business_id: str, instrument: str, network_bps: int,
                 platform_bps: int) -> None:
        """
        Change a negotiated rate.

        A regulated rate cannot be raised above its cap, whatever anyone types.
        If a merchant could enter "UPI network MDR: 90bps" the auditor would
        stop reporting zero-MDR violations - it would have been told they are
        contractual. The one rule the merchant does not get to edit is the one
        Parliament wrote.
        """
        spec = self.conn.execute(
            "SELECT * FROM business_rate_card WHERE business_id = ? AND instrument = ?",
            (business_id, instrument)).fetchone()
        if spec is None:
            raise KeyError(f"{business_id} has no instrument {instrument}")

        cap = spec["network_mdr_cap_bps"]
        if cap is not None and network_bps > cap:
            raise ValueError(
                f"{spec['label']} is capped at {cap / 100:.2f}% by "
                f"{REGULATED.get(instrument, 'regulation')}. "
                f"A contract cannot agree to more.")
        if network_bps < 0 or platform_bps < 0:
            raise ValueError("rates cannot be negative")

        self.conn.execute(
            "UPDATE business_rate_card SET network_mdr_bps = ?,"
            " platform_fee_bps = ? WHERE business_id = ? AND instrument = ?",
            (network_bps, platform_bps, business_id, instrument))
        self.conn.commit()

    def set_guardrails(self, business_id: str, min_confidence: float,
                       review_above_paise: int) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("confidence threshold must be between 0 and 1")
        if review_above_paise < 0:
            raise ValueError("the review threshold cannot be negative")
        self.conn.execute(
            "UPDATE businesses SET min_confidence = ?, review_above_paise = ?"
            " WHERE business_id = ?",
            (min_confidence, review_above_paise, business_id))
        self.conn.commit()

    def set_behaviour(self, business_id: str, behaviour: str) -> None:
        self.conn.execute(
            "UPDATE businesses SET gateway_behaviour = ? WHERE business_id = ?",
            (behaviour, business_id))
        self.conn.commit()

    def behaviour(self, business_id: str) -> str:
        row = self.get(business_id)
        return row["gateway_behaviour"] if row else "correct"

    # How the SIMULATED suppliers file. A demo control, kept beside the
    # gateway's fault switch for the same reason: it belongs to the simulator
    # and not to a business, and putting it on the purchase form asked a
    # merchant to declare something only the product can find out.
    def set_supplier_behaviour(self, business_id: str, behaviour) -> None:
        """
        Store one behaviour, or several.

        Several are kept comma-separated in the same column rather than in a
        table of their own. A row written before this feature holds a bare
        value and parses as a one-element list, so nothing had to be migrated -
        and the single-choice case stays the one-element case rather than
        becoming a separate code path.
        """
        from merchant.suppliers import join_behaviours

        self.conn.execute(
            "UPDATE businesses SET supplier_behaviour = ? WHERE business_id = ?",
            (join_behaviours(behaviour), business_id))
        self.conn.commit()

    def supplier_behaviour(self, business_id: str) -> str:
        row = self.get(business_id)
        try:
            return row["supplier_behaviour"] or "correct"
        except (TypeError, IndexError, KeyError):
            return "correct"

    def supplier_behaviours(self, business_id: str) -> list:
        """The stored setting as a list, however many are switched on."""
        from merchant.suppliers import parse_behaviours

        return parse_behaviours(self.supplier_behaviour(business_id))

    # --- agents -----------------------------------------------------------

    def enabled_agents(self, business_id: str) -> set[str]:
        return {r["agent_id"] for r in self.conn.execute(
            "SELECT agent_id FROM business_agents"
            " WHERE business_id = ? AND enabled = 1", (business_id,))}

    def agent_enabled(self, business_id: str, agent_id: str) -> bool:
        row = self.conn.execute(
            "SELECT enabled FROM business_agents"
            " WHERE business_id = ? AND agent_id = ?",
            (business_id, agent_id)).fetchone()
        return bool(row["enabled"]) if row else False

    def set_agent(self, business_id: str, agent_id: str, enabled: bool) -> None:
        self.conn.execute(
            "INSERT INTO business_agents (business_id, agent_id, enabled, enabled_at)"
            " VALUES (?,?,?,?) ON CONFLICT(business_id, agent_id) DO UPDATE SET"
            " enabled = excluded.enabled, enabled_at = excluded.enabled_at",
            (business_id, agent_id, int(enabled), int(time.time())))
        self.conn.commit()
