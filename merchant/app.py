"""
Ledgerline - agentic financial services for Indian merchants.

  ./run_platform.sh        then open http://localhost:8000

One agent is live: the settlement deduction auditor. The rest of the catalogue
is declared and unbuilt, and says so. See merchant/catalog.py for why that
honesty is load-bearing.

## Multi-tenant, and what that does and does not mean

Businesses are first-class. Each has its own rate card, its own gateway
behaviour, its own books, and its own settlements. Everything is scoped by
business_id and the scoping is tested directly.

What it does NOT mean is authenticated. CLAUDE.md section 16 says not to build
auth and that is the right call for the time available - but an unauthenticated
multi-tenant app should say so out loud rather than imply a boundary it does not
have. The About page says it, in those words.

## Where the auditing happens

Nowhere in this file. Routes resolve a business, look up the agent in the
catalogue, and hand it a context. The audit logic lives in engine/ and agent/
and is covered by the test suite. A web layer that reimplemented any of it would
be a second system to keep correct.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import merchant.agents.gst  # noqa: F401  - registers the live agent
import merchant.agents.settlement  # noqa: F401  - registers the live agent
from engine.expected_value import rupees
from merchant import catalog, views
from merchant.accesslog import ACTION_LABEL, AccessLog, Action
from merchant.auth import SESSION_COOKIE, Auth, Role, User
from merchant.catalog import AgentContext
from merchant.gateway import BEHAVIOUR_LABEL, BEHAVIOUR_NOTE, Behaviour
from merchant import benchmark as benchmark_mod
from engine.gst import rules as rules_gst
from merchant import nav, ui
from merchant.ledger import Ledger

DB = os.environ.get("AUDITOR_DB", str(Path(__file__).parent.parent / "merchant.db"))
COOKIE = "business_id"

app = FastAPI(title="Ledgerline")

# agent runs in flight, keyed by target. In-process on purpose: this is a
# single-operator tool, and a job queue would be a moving part with nothing to do.
RUNS: dict[str, dict] = {}
_lock = threading.Lock()


def ledger(business_id: Optional[str] = None) -> Ledger:
    return Ledger(DB, business_id)


def _shell(led: Ledger) -> dict:
    """
    Everything the frame needs, in one place.

    Gathered once per request rather than per page, so no page can accidentally
    render without the business selector or the gateway mode indicator - the two
    pieces of context that stop someone acting on the wrong books or mistaking
    simulated deductions for real ones.
    """
    current = led.businesses.get(led.business_id) if led.business_id else None
    if current is None:
        return {"business": None, "businesses": led.businesses.all()}
    from merchant.sources import Sources

    kind = Sources(led.conn).kind(led.business_id)
    return {
        "business": current,
        "businesses": led.businesses.all(),
        "behaviour": led.behaviour(),
        "agents": catalog.all_agents(),
        "enabled": led.businesses.enabled_agents(led.business_id),
        "source": str(kind) if kind else None,
    }


def _shell_for(led: Ledger, ws: Optional[Workspace]) -> dict:
    """
    The shell, scoped to one person.

    A user sees only the businesses they belong to, and the rail only offers
    what their role can actually use - a control that is visible and then
    refuses you is worse than one that was never there.
    """
    shell = _shell(led)
    if ws is None:
        return shell
    with ledger() as fresh:
        shell["businesses"] = Auth(fresh.conn).businesses_for(ws.user)
    shell["viewer"] = ws.user
    shell["role"] = ws.role
    return shell


# --- who is asking, and what they may do ---------------------------------
#
# Three roles, three failure modes, and they are not the same failure:
#
#   not logged in    -> send them to the login page
#   logged in, not a member of this business -> 404, not 403. Confirming a
#                       business exists to someone with no business being there
#                       is itself a leak.
#   member, wrong role -> 403 with a reason, because they are entitled to know
#                       why a control they can see is refusing them.


class NeedsLogin(Exception):
    pass


class NeedsRole(Exception):
    def __init__(self, message: str):
        self.message = message


@app.exception_handler(NeedsLogin)
def _needs_login(request: Request, exc: NeedsLogin):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(NeedsRole)
def _needs_role(request: Request, exc: NeedsRole):
    return HTMLResponse(views.error_page(
        "Not your decision to make", exc.message, "Back", "/"), status_code=403)


def current_user(session: Optional[str] = Cookie(None)) -> User:
    with ledger() as led:
        user = Auth(led.conn).user_for(session)
    if user is None:
        raise NeedsLogin()
    return user


def maybe_user(session: Optional[str] = Cookie(None)) -> Optional[User]:
    with ledger() as led:
        return Auth(led.conn).user_for(session)


def operator(user: User = Depends(current_user)) -> User:
    if not user.is_operator:
        raise NeedsRole("That page belongs to whoever runs this platform, not "
                        "to a business on it.")
    return user


class Workspace:
    """The business being looked at, and what this user may do in it."""

    def __init__(self, user: User, business_id: str, role: Role):
        self.user = user
        self.business_id = business_id
        self.role = role

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER

    def audit(self, action: Action, request=None, target=None, allowed=True,
              detail=None) -> None:
        from merchant.ratelimit import client_address

        with ledger() as led:
            AccessLog(led.conn).record(
                action, user=self.user, business_id=self.business_id,
                target=target, allowed=allowed, detail=detail,
                address=client_address(request) if request is not None else "")

    def require_owner(self, what: str, request=None,
                      action: Action = Action.CHANGE_RATE_CARD) -> None:
        # The action is a parameter because the log has to say what was
        # actually attempted. A blocked delete recorded as "changed the rate
        # card" is a false entry, and a log that is wrong about the small
        # things is not evidence about the large ones.
        if not self.is_owner:
            self.audit(action, request, target=what,
                       allowed=False, detail="staff attempted an owner action")
            raise NeedsRole(
                f"Changing {what} is the owner's decision. Every finding in "
                f"this product is 'you were charged more than your contract "
                f"says', so whoever can edit the contract can silently switch "
                f"findings off. You are signed in as staff.")


def workspace(request: Request, user: User = Depends(current_user),
              business_id: Optional[str] = Cookie(None)) -> Optional[Workspace]:
    """The current workspace, or None when the user has not picked one."""
    if not business_id:
        return None
    with ledger() as led:
        auth = Auth(led.conn)
        row = led.businesses.get(business_id)
        if row is None:
            return None
        if row["archived_at"]:
            # Put away on purpose. Its books stay exactly as they were and
            # nothing may be added to them until somebody restores it.
            return None
        role = auth.role_in(user, business_id)
        if role is None:
            # Someone holding a cookie for a business they are not in. Routine
            # after a membership is removed, and exactly the event an operator
            # would want to see if it is not routine.
            from merchant.ratelimit import client_address

            AccessLog(led.conn).denied(
                Action.SWITCH_BUSINESS, user=user, business_id=business_id,
                address=client_address(request),
                detail="held a cookie for a business they are not a member of")
    if role is None:
        return None                     # not a member: as good as not existing
    return Workspace(user, business_id, role)


def required_workspace(ws: Optional[Workspace] = Depends(workspace)) -> Workspace:
    if ws is None:
        raise NeedsRole("Pick a business first.")
    return ws


def _resolve(business_id: Optional[str]) -> Optional[str]:
    """A cookie pointing at a deleted business is not a business."""
    with ledger() as led:
        if business_id and led.businesses.get(business_id):
            return business_id
    return None


# --- signing in ------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = "", user: Optional[User] = Depends(maybe_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)

    with ledger() as led:
        first_run = not Auth(led.conn).any_users()
    if first_run:
        return RedirectResponse("/signup", status_code=303)

    banner = (f'<div class="banner warn"><span>{views.esc(error)}</span></div>'
              if error else "")
    return HTMLResponse(views.auth_page("Sign in", "Welcome back.", f"""
    {banner}
    <form method="post" action="/login">
      <div style="margin-bottom:11px"><label>Email</label>
        <input name="email" type="email" required autofocus></div>
      <div style="margin-bottom:15px"><label>Password</label>
        <input name="password" type="password" required></div>
      <button style="width:100%">Sign in</button>
    </form>""", 'No account? <a href="/signup">Create one</a>'))


@app.post("/login")
def do_login(request: Request, email: str = Form(...), password: str = Form(...)):
    from urllib.parse import quote

    from merchant.ratelimit import RateLimit, client_address

    address = client_address(request)
    with ledger() as led:
        limiter = RateLimit(led.conn)
        throttled = limiter.check_login(address, email)
        if throttled is not None:
            # Checked BEFORE the password is verified, so a throttled attempt
            # costs nothing and reveals nothing.
            return RedirectResponse(
                f"/login?error={quote(f'Too many attempts. Try again in {throttled.human}.')}",
                status_code=303)

        token = Auth(led.conn).login(email, password)
        if token is None:
            limiter.record_failure(address, email)
        else:
            limiter.record_success(address, email)

    if token is None:
        # One message for both causes. Saying "no such account" tells anyone
        # who asks which email addresses are registered here.
        return RedirectResponse(
            f"/login?error={quote('Email or password is wrong.')}",
            status_code=303)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, max_age=30 * 86_400,
                        httponly=True, samesite="lax")
    return response


@app.get("/signup", response_class=HTMLResponse)
def signup_page(error: str = "", user: Optional[User] = Depends(maybe_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)

    with ledger() as led:
        first_run = not Auth(led.conn).any_users()

    note = ""
    if first_run:
        note = ('<div class="banner brand"><b>First account</b><span>Nobody has '
                'signed up yet, so this account runs the platform: it can see '
                'every business and decide which agents are live.</span></div>')

    banner = (f'<div class="banner warn"><span>{views.esc(error)}</span></div>'
              if error else "")
    return HTMLResponse(views.auth_page(
        "Create an account", "Ledgerline audits what your gateway deducted.",
        f"""
    {note}{banner}
    <form method="post" action="/signup">
      <div style="margin-bottom:11px"><label>Your name</label>
        <input name="name" autofocus></div>
      <div style="margin-bottom:11px"><label>Email</label>
        <input name="email" type="email" required></div>
      <div style="margin-bottom:15px"><label>Password</label>
        <input name="password" type="password" minlength="8" required>
        <p class="sub" style="margin:5px 0 0;font-size:11.5px">
          At least 8 characters.</p></div>
      <button style="width:100%">Create account</button>
    </form>""", 'Already have one? <a href="/login">Sign in</a>'))


@app.post("/signup")
def do_signup(request: Request, email: str = Form(...),
              password: str = Form(...), name: str = Form("")):
    from urllib.parse import quote

    from merchant.ratelimit import RateLimit, client_address

    address = client_address(request)
    with ledger() as led:
        limiter = RateLimit(led.conn)
        throttled = limiter.check_signup(address)
        if throttled is not None:
            return RedirectResponse(
                f"/signup?error={quote(f'Too many accounts created from here. Try again in {throttled.human}.')}",
                status_code=303)

        auth = Auth(led.conn)
        try:
            user = auth.register(email, password, name)
        except ValueError as exc:
            # Recorded even when it fails, so the form cannot be used to probe
            # which addresses are already registered.
            limiter.record_failure(address, email, kind="signup")
            return RedirectResponse(f"/signup?error={quote(str(exc))}",
                                    status_code=303)
        limiter.record_failure(address, email, kind="signup")
        token = auth.start_session(user.user_id)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, max_age=30 * 86_400,
                        httponly=True, samesite="lax")
    return response


@app.get("/logout")
def do_logout(session: Optional[str] = Cookie(None)):
    with ledger() as led:
        Auth(led.conn).logout(session)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(COOKIE)
    return response


# --- choosing a business --------------------------------------------------

@app.get("/businesses", response_class=HTMLResponse)
def businesses_page(user: User = Depends(current_user),
                    ws: Optional[Workspace] = Depends(workspace),
                    error: str = "", ok: str = ""):
    """
    A user sees the businesses they belong to, and no others. An operator sees
    the same list - running the platform is not the same as being entitled to
    open a customer's books, and /admin is where cross-tenant sight belongs.
    """
    with ledger(ws.business_id if ws else None) as led:
        shell = _shell_for(led, ws)
        shell["businesses"] = Auth(led.conn).businesses_for(user)
        put_away = Auth(led.conn).archived_for(user)

    rows = "".join(f"""
      <tr>
        <td><a href="/switch?business_id={views.esc(b["business_id"])}"
          style="color:var(--ink);font-weight:{600 if shell.get("business") and
          b["business_id"] == shell["business"]["business_id"] else 400}">
          {views.esc(b["name"])}</a></td>
        <td><span class="pill">{views.esc(b["role"])}</span></td>
        <td class="r">{b["payments"]}</td>
        <td class="r">{views.when(b["created_at"])}</td>
        <td class="r"><a class="btn ghost small"
          href="/switch?business_id={views.esc(b["business_id"])}">open</a></td>
      </tr>""" for b in shell["businesses"])

    archived_rows = "".join(f"""
      <tr>
        <td style="color:var(--muted)">{views.esc(b["name"])}</td>
        <td><span class="pill">{views.esc(b["role"])}</span></td>
        <td class="r" style="color:var(--muted)">{views.when(b["archived_at"])}</td>
        <td class="r">{
          '<form method="post" action="/businesses/restore" style="display:inline">'
          f'<input type="hidden" name="business_id" value="{views.esc(b["business_id"])}">'
          '<button class="ghost small">restore</button></form>'
          if b["role"] == str(Role.OWNER) else ''}</td>
      </tr>""" for b in put_away)

    archived_card = f"""
<div class="card flush">
  <div class="card-head"><h2>Archived</h2></div>
  <table>
    <tr><th>Name</th><th>Your role</th><th class="r">Archived</th>
        <th class="r"></th></tr>
    {archived_rows}
  </table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    Closed to new sales, and out of the switcher. Every settlement, finding and
    agent decision is exactly where it was.
  </div>
</div>""" if put_away else ""

    banner = ""
    if error:
        banner = f'<div class="banner warn"><span>{views.esc(error)}</span></div>'
    elif ok:
        banner = f'<div class="banner brand"><span>{views.esc(ok)}</span></div>'

    body = f"""
{banner}
<h1>Businesses</h1>
<p class="sub">Each business has its own contract with its gateway, its own
   books, and its own settlements. Nothing is shared between them.</p>

<div class="card">
  <form method="post" action="/businesses">
    <div class="row">
      <div><label>Business name</label>
        <input name="name" placeholder="Ravi Electronics" required></div>
      <div style="flex:0"><button>Create business</button></div>
    </div>
    <p class="sub" style="margin:12px 0 0">It starts on the reference rate card
       &mdash; the RBI-capped rates and a typical negotiated slab. Change the
       negotiated ones in Settings to match a real contract.</p>
  </form>
</div>

<div class="card"><table>
  <tr><th>Name</th><th>Your role</th><th class="r">Payments</th>
      <th class="r">Created</th><th class="r"></th></tr>
  {rows or '<tr><td colspan="5" class="empty">'
   '<div style="font-weight:560;color:var(--ink);margin-bottom:4px">'
   'You are not in any business yet</div>'
   'Create one above, or ask an owner to add you.</td></tr>'}
</table></div>

{archived_card}"""
    return views.page("Businesses", body, "businesses", **shell)


@app.post("/businesses")
def create_business(name: str = Form(...), user: User = Depends(current_user)):
    with ledger() as led:
        try:
            new_id = led.businesses.create(name)
            # Whoever creates it owns it. Someone has to be able to correct the
            # rate card, and a business with no owner is one nobody can fix.
            Auth(led.conn).add_member(new_id, user.user_id, Role.OWNER)
        except ValueError as exc:
            return HTMLResponse(views.error_page(
                "That name will not work", str(exc), "Back", "/businesses"),
                status_code=400)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, new_id, max_age=60 * 60 * 24 * 365)
    return response


@app.get("/switch")
def switch(business_id: str, user: User = Depends(current_user)):
    """Switching to a business you are not a member of is not switching."""
    with ledger() as led:
        if Auth(led.conn).role_in(user, business_id) is None:
            return RedirectResponse("/businesses", status_code=303)
        if led.businesses.is_archived(business_id):
            return RedirectResponse("/businesses", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, business_id, max_age=60 * 60 * 24 * 365)
    return response


# --- sales ----------------------------------------------------------------

INSTRUMENTS = {
    "upi": ("upi", None, None, False),
    "rupay_debit": ("card", "rupay", "debit", False),
    "visa_debit": ("card", "visa", "debit", False),
    "visa_credit": ("card", "visa", "credit", False),
    "amex": ("card", "amex", "credit", False),
    "international": ("card", "visa", "credit", True),
    "netbanking": ("netbanking", None, None, False),
    "wallet": ("wallet", None, None, False),
}


@app.get("/data/simulator", response_class=HTMLResponse)
def simulator_page(ws: Workspace = Depends(required_workspace)):  # noqa: D401
    """
    The stand-in for a data connection.

    In production a merchant's settlements arrive over the Razorpay API and
    nobody types anything. This page exists so the auditor has something to
    audit without a connected account, and it is labelled demo everywhere it
    appears - because an app whose first screen is a sales form looks like a
    point-of-sale system, which is the opposite of what this is.
    """
    # The same control for the other side of the books. Written out rather than
    # shared with the gateway's loop: they carry different explanations, and a
    # helper taking both would spend more lines on its parameters than the
    # duplication costs.
    from merchant.suppliers import (BEHAVIOUR_FINDS as SUPPLIER_FINDS,
                                    BEHAVIOUR_LABEL as SUPPLIER_LABEL,
                                    BEHAVIOUR_NOTE as SUPPLIER_NOTE,
                                    SupplierBehaviour)

    resolved = ws.business_id

    with ledger(resolved) as led:
        shell = _shell_for(led, ws)
        supplier_behaviour = SupplierBehaviour(
            led.businesses.supplier_behaviour(resolved))
        behaviour = led.behaviour()
        pending = led.unsettled()
        orders = led.orders(limit=25)
        audit_on = led.businesses.agent_enabled(resolved, "settlement_audit")
    gross = sum(p["amount"] for p in pending)
    deducted = sum(p["fee"] + p["tax"] for p in pending)

    rows = "".join(f"""
      <tr>
        <td class="mono">{views.esc(o["payment_id"] or "-")}</td>
        <td>{views.esc(o["description"] or "-")}</td>
        <td>{views.esc(o["paid_method"] or "-")}</td>
        <td class="r">{rupees(o["amount"])}</td>
        <td class="r">{rupees((o["fee"] or 0) + (o["tax"] or 0))}</td>
        <td class="r">{views.when(o["created_at"])}</td>
        <td class="r">{
          '<span class="pill good">settled</span>' if o["settled_run_id"]
          else '<span class="pill warn">refunded</span>' if o["refunded"]
          else (f'<span class="pill brand">awaiting</span> '
                f'<form method="post" action="/refund" style="display:inline">'
                f'<input type="hidden" name="payment_id" value="{views.esc(o["payment_id"])}">'
                f'<button class="ghost small">refund</button></form>')
          if o["payment_id"] else '<span class="pill">unpaid</span>'}</td>
      </tr>""" for o in orders)

    # The fault switch lives HERE, not in the merchant's settings. No real
    # merchant has a "make my gateway misbehave" control; it is a demo
    # instrument, and putting it on a customer-facing settings page was the
    # single most confusing thing in the app.
    from merchant.gateway import BEHAVIOUR_AFFECTS, BEHAVIOUR_FINDS

    faults = "".join(f"""
      <label style="display:block;padding:9px 12px;border:1px solid
        {'var(--brand)' if b == behaviour else 'var(--line)'};border-radius:7px;
        margin-bottom:5px;cursor:pointer;
        {'background:var(--brand-wash);' if b == behaviour else ''}">
        <div style="display:flex;align-items:center;gap:9px">
          <input type="radio" name="behaviour" value="{b.value}"
            {'checked' if b == behaviour else ''} style="width:auto;margin:0">
          <b style="font-size:12.5px">{views.esc(BEHAVIOUR_LABEL[b])}</b>
          <span class="sp"></span>
          <span class="pill {'good' if b == Behaviour.CORRECT else 'danger'}">
            finds {views.esc(BEHAVIOUR_FINDS[b])}</span>
        </div>
        <div style="color:var(--muted);font-size:11.3px;margin:3px 0 0 25px">
          {views.esc(BEHAVIOUR_NOTE[b])}
          <b>{views.esc("Applies to " + ", ".join(BEHAVIOUR_AFFECTS[b]) + "."
                        if BEHAVIOUR_AFFECTS[b]
                        else "Nothing is charged incorrectly.")}</b>
          Payments on other rails stay clean &mdash; the auditor is not missing
          them, there is nothing there to find.
        </div>
      </label>""" for b in Behaviour)

    supplier_faults = "".join(f"""
      <label style="display:block;padding:9px 12px;border:1px solid
        {'var(--brand)' if b == supplier_behaviour else 'var(--line)'};
        border-radius:7px;margin-bottom:5px;cursor:pointer;
        {'background:var(--brand-wash);' if b == supplier_behaviour else ''}">
        <div style="display:flex;align-items:center;gap:9px">
          <input type="radio" name="behaviour" value="{b.value}"
            {'checked' if b == supplier_behaviour else ''}
            style="width:auto;margin:0">
          <b style="font-size:12.5px">{views.esc(SUPPLIER_LABEL[b])}</b>
          <span class="sp"></span>
          <span class="pill {'good' if b == SupplierBehaviour.CORRECT else 'danger'}">
            finds {views.esc(SUPPLIER_FINDS[b])}</span>
        </div>
        <div style="color:var(--muted);font-size:11.3px;margin:3px 0 0 25px">
          {views.esc(SUPPLIER_NOTE[b])}
        </div>
      </label>""" for b in SupplierBehaviour)

    fault_panel = f"""
<div class="card">
  <h2>How the gateway behaves</h2>
  <p class="sub" style="margin:3px 0 12px">An auditor with nothing to catch
     demonstrates nothing, so the fault is a switch you can see rather than
     something hidden in a fixture. No real merchant has this control &mdash;
     it belongs to the simulator, not to a business.</p>
  <form method="post" action="/settings/gateway">
    {faults}
    <button style="margin-top:4px">Apply to new payments</button>
  </form>
  <p class="sub" style="margin:10px 0 0;font-size:11.3px">Payments already taken
     keep whatever was deducted at the time.</p>
</div>

<div class="card">
  <h2>How your suppliers file</h2>
  <p class="sub" style="margin:3px 0 12px">The other side of the books. A real
     merchant has no idea whether a supplier filed &mdash; finding out is the
     whole point of the input credit agent &mdash; so this switch belongs to
     the simulator, not to a purchase form.</p>
  <form method="post" action="/settings/suppliers">
    {supplier_faults}
    <button style="margin-top:4px">Apply to new invoices</button>
  </form>
  <p class="sub" style="margin:10px 0 0;font-size:11.3px">Invoices already
     recorded keep whatever their supplier did at the time.</p>
</div>"""

    settle = ""
    if pending:
        settle = f"""
      <form method="post" action="/settle">
        <button>Settle {len(pending)} payment{'s' if len(pending) != 1 else ''}
          &rarr; {rupees(gross - deducted)}</button>
      </form>"""

    warn = "" if audit_on else (
        '<div class="banner warn">The settlement auditor is turned off for this '
        'business. Settlements will not be checked. '
        '<a href="/agents" style="color:inherit">Turn it on</a></div>')

    body = f"""
<div class="banner brand"><b>Demo data</b><span>This is not where sales
  happen. It stands in for a connected gateway, so the auditor has settlements
  to work on. In production these arrive over the API and nobody types
  anything. <a href="/data">Change data source</a></span></div>
{warn}{views.behaviour_banner(behaviour)}
<h1>Simulator &mdash; {views.esc(shell["business"]["name"])}</h1>
<p class="sub">A customer pays. The gateway takes its cut. Nothing checks
   whether that cut is correct &mdash; that only happens after settlement,
   which is the whole problem.</p>

{fault_panel}

<div class="card">
  <form method="post" action="/sale">
    <div class="row">
      <div><label>Amount (&#8377;)</label>
        <input name="rupees" type="number" step="0.01" min="0.01"
          value="1627.00" required></div>
      <div><label>What was sold</label>
        <input name="description" placeholder="Silk scarf" value="Silk scarf"></div>
      <div><label>Paid with</label>
        <select name="instrument">
          <option value="upi">UPI</option>
          <option value="rupay_debit">RuPay debit card</option>
          <option value="visa_debit">Visa/Mastercard debit</option>
          <option value="visa_credit">Visa/Mastercard credit</option>
          <option value="amex">Amex</option>
          <option value="international">International card</option>
          <option value="netbanking">Net banking</option>
          <option value="wallet">Wallet</option>
        </select></div>
      <div style="flex:0"><button>Take payment</button></div>
    </div>
  </form>
</div>

<div class="card flush">
  <div class="card-head">
    <div><h2>Recent sales</h2>
      <p class="sub" style="margin:2px 0 0">{
        f"{len(pending)} awaiting settlement &middot; {rupees(gross)} gross, "
        f"{rupees(deducted)} already deducted"
        if pending else "Everything here has been settled."}</p></div>
    <span class="sp"></span>
    {settle}
  </div>
  <table>
    <tr><th>Payment</th><th>Item</th><th>Method</th><th class="r">Amount</th>
        <th class="r">Deducted</th><th class="r">When</th><th class="r">Status</th></tr>
    {rows or '<tr><td colspan="7" class="empty">'
     '<div style="font-weight:560;color:var(--ink);margin-bottom:4px">'
     'No sales yet</div>'
     'Record one above. The gateway will deduct its fee, and once you settle '
     'the batch the auditor checks whether that fee was right.</td></tr>'}
  </table>
</div>"""
    return views.page("Simulator", body, "data", **shell)


def _welcome(user: User) -> str:
    """Signed in, but not in any business yet."""
    live = [a for a in catalog.all_agents() if a.is_live]
    planned = [a for a in catalog.all_agents() if not a.is_live]
    return views.page("Ledgerline", f"""
<div class="card" style="padding:34px">
  <h1>Welcome, {views.esc(user.name)}</h1>
  <p class="sub" style="max-width:620px;font-size:13.5px">
    Your money already flows through your payment gateway. Ledgerline reads what
    the gateway <i>did</i> to it &mdash; and tells you which parts were wrong.
    It is not where sales happen. It is where they get checked afterwards.</p>
  <form method="post" action="/businesses" style="margin-top:20px">
    <div class="row" style="max-width:520px">
      <div><label>Set up your first business</label>
        <input name="name" placeholder="Meera&rsquo;s Boutique" required autofocus></div>
      <div style="flex:0"><button>Get started</button></div>
    </div>
  </form>
  <p class="sub" style="margin:12px 0 0;font-size:11.5px">You will be its owner,
     which means you can correct its rate card. Everything the auditor reports is
     measured against that contract.</p>
</div>
<div class="card">
  <h2>Available now</h2>
  <p class="sub">{views.esc(live[0].name) if live else '-'} &mdash;
     {views.esc(live[0].tagline) if live else ''}</p>
  <h2 style="margin-top:16px">On the roadmap, not yet built</h2>
  <p class="sub" style="margin:0">{views.esc(', '.join(a.name for a in planned))}</p>
</div>""", viewer=user)


@app.get("/", response_class=HTMLResponse)
def overview_page(user: User = Depends(current_user),
                  ws: Optional[Workspace] = Depends(workspace)):
    """
    The front door, and it is deliberately not a form.

    Settlements in, findings out - that is what the product does, so that is
    what the first screen shows. The previous front door was a sales form,
    which made a settlement auditor look like a point-of-sale system.
    """
    if ws is None:
        return HTMLResponse(_welcome(user))
    resolved = ws.business_id

    from merchant.sources import KIND_BLURB, KIND_LABEL, SourceKind, Sources

    with ledger(resolved) as led:
        shell = _shell_for(led, ws)
        sources = Sources(led.conn)
        source = sources.get(resolved)
        runs = led.settlements()
        totals = [led.store.totals(r["run_id"]) for r in runs]
        credited_by = {r["run_id"]: led.conn.execute(
            "SELECT COALESCE(SUM(amount),0) a FROM bank_credits WHERE run_id = ?",
            (r["run_id"],)).fetchone()["a"] for r in runs}
        open_findings = []
        for run in runs:
            for f in led.store.findings(run["run_id"], queued_only=True):
                open_findings.append((run["run_id"], f))

    if source is None:
        return RedirectResponse("/data", status_code=303)

    audited = [r for r in runs if r["findings"]]
    recoverable = sum(t["recoverable_paise"] for t in totals)
    records = sum(t["n"] for t in totals)
    queued = sum(t["queued"] for t in totals)

    # --- nothing to show yet -----------------------------------------------
    #
    # A screen of zeros is not neutral. It reads as broken, and it is the first
    # impression every new business gets. Say what this screen will show once
    # there is data, and give exactly one next action.
    if not runs:
        simulated = source["kind"] == str(SourceKind.SIMULATOR)
        steps = [
            ("done", "Business created",
             f'{shell["business"]["name"]} is set up on the reference rate card. '
             f'Adjust it in Settings to match your real contract.', "", ""),
            ("done", f"Reading from {KIND_LABEL[SourceKind(source['kind'])]}",
             KIND_BLURB[SourceKind(source["kind"])], "", ""),
        ]
        if simulated:
            steps.append(
                ("now", "Take a payment, then settle it",
                 "The gateway takes its cut. Nothing checks whether that cut is "
                 "correct - that only happens after settlement, which is the "
                 "whole problem.", "Open the simulator", "/data/simulator"))
        else:
            steps.append(
                ("now", "Pull your settlements",
                 "Reads the settlement recon report from Razorpay. Test mode "
                 "does not settle, so a test account will find nothing yet.",
                 "Sync now", "/data"))
        steps.append(
            ("later", "Run the auditor",
             "Your rate card settles most records instantly. Anything needing "
             "judgment goes to the agent, and whatever it finds appears here.",
             "", ""))

        body = f"""
<h1>{views.esc(shell["business"]["name"])}</h1>
<p class="sub">Nothing has been settled yet, so there is nothing to audit.
   Here is what happens next.</p>

<div class="card">{views.checklist(steps)}</div>

<div class="card tint">
  <h2>What this screen will show</h2>
  <p class="sub" style="margin:4px 0 0">Once a settlement has been audited:
     how much was deducted that should not have been, which findings need your
     decision, and a paste-ready letter for each one you choose to dispute.
     Every figure computed from your own rate card and the regulation behind
     it &mdash; never by a language model.</p>
</div>"""
        return views.page("Overview", body, "overview", **shell)

    # --- the executive view ------------------------------------------------
    #
    # This screen used to be the settlement agent's dashboard wearing the name
    # "Overview": its tables were settlement tables, and it carried an "Ask the
    # auditor" box that could only ever answer questions about settlements.
    # With a second agent live that stopped being a simplification and became
    # a lie about what the product does.
    #
    # What belongs here is only what is true ACROSS agents: the totals, how
    # each one is doing, and the single queue of things waiting on a person.
    with ledger(resolved) as led:
        picture = _agent_picture(led, ws)
        decisions = _open_decisions(led, ws)

    recoverable = sum(t["recoverable_paise"] for t in totals)
    records = sum(t["n"] for t in totals)
    itc_exposed = 0
    last_check = None
    with ledger(resolved) as led:
        last_check = led.last_check()
    if last_check:
        itc_exposed = last_check["exposed_paise"]

    live_count = sum(1 for spec, _r, _st, _m in picture if spec.is_live)

    metrics = ui.metric_bar([
        (rupees(recoverable), "recoverable from your gateway"),
        (rupees(itc_exposed), "input credit at risk"),
        (f"{records:,}", "records audited"),
        (str(len(decisions)), "waiting on you"),
        (str(live_count), "agents running"),
    ])

    # --- one queue, all agents ---------------------------------------------
    if decisions:
        rows = "".join(f"""
      <tr>
        <td>{ui.badge(d["agent"], ui.TONE_BRAND)}</td>
        <td>{views.esc(d["what"])}
          <div style="color:var(--muted);font-size:11.5px;margin-top:2px">
            {views.esc(d["why"])}</div></td>
        <td class="r">{rupees(d["amount"])}</td>
        <td class="r"><a class="btn ghost small" href="{views.esc(d["href"])}">
          Decide</a></td>
      </tr>""" for d in decisions)
        queue = f"""
<div class="card flush" style="border-left:3px solid var(--warn)">
  <div class="card-head"><h2>Needs your decision</h2>
    <span class="sub">{len(decisions)} across {live_count} agents</span></div>
  <table>
    <tr><th>Agent</th><th>What</th><th class="r">Amount</th><th class="r"></th></tr>
    {rows}
  </table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    Nothing here has been acted on. Every one is a proposal waiting for a
    person, which is the only state an agent decision is ever allowed to reach
    by itself.
  </div>
</div>"""
    else:
        queue = ui.card(
            '<p class="sub" style="margin:0">Nothing is waiting on you. '
            'Findings the agents were confident about were closed on their '
            'own; anything they were unsure about, or anything large, would '
            'appear here instead.</p>',
            title="Needs your decision")

    cards = []
    for spec, route, state, agent_metrics in picture:
        if not spec.is_live:
            continue
        cards.append(ui.agent_card(ui.AgentCardData(
            name=spec.name, tagline=spec.tagline, state=state,
            href=route.href if route else "#", metrics=agent_metrics,
            cta="Open workspace" if state != ui.STATE_SETUP else "Set it up",
            why_unbuilt=spec.why_unbuilt)))

    body = f"""
<h1>{views.esc(shell["business"]["name"])}</h1>
<p class="sub">Everything running across your books, and what is waiting on
   you.</p>

{metrics}

<div style="margin:22px 0 9px;font-size:11.5px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted);font-weight:600">Your agents</div>
{ui.grid(cards)}

<div style="margin-top:22px">{queue}</div>

<div style="margin-top:9px">
  <a class="btn ghost small" href="/agents">See every agent, including what is
    coming</a>
</div>"""
    return views.page("Home", body, "home", **shell)


def _open_decisions(led, ws) -> list:
    """
    Everything waiting on a person, from every agent, in one list.

    Deliberately not per-agent queues stitched together at render time: a
    merchant has one afternoon, not one afternoon per agent, and the question
    they are actually asking is "what needs me", not "what does the settlement
    auditor think".
    """
    out = []

    for run in led.settlements():
        for finding in led.store.findings(run["run_id"], queued_only=True):
            out.append({
                "agent": "Settlement",
                "what": finding["reasoning"] or finding["exception_code"],
                "why": finding["exception_code"].replace("_", " ").lower(),
                "amount": finding["money_at_stake"] or 0,
                "href": f"/agents/settlement/run/{run['run_id']}",
            })

    last = led.last_check()
    if last:
        for raised in led.raised_in(last["check_id"]):
            out.append({
                "agent": "Input credit",
                "what": raised["headline"],
                "why": f'{raised["urgency"].replace("_", " ")} · '
                       f'{raised["action"].replace("_", " ")}',
                "amount": raised["exposed_paise"] or 0,
                "href": "/agents/input-credit",
            })

    out.sort(key=lambda d: -d["amount"])
    return out




# --- where the data comes from -------------------------------------------

@app.get("/data", response_class=HTMLResponse)
def sources_page(ws: Workspace = Depends(required_workspace),
                 error: str = "", ok: str = ""):
    resolved = ws.business_id

    from merchant.sources import KIND_BLURB, KIND_LABEL, SourceKind, Sources

    with ledger(resolved) as led:
        shell = _shell_for(led, ws)
        current = Sources(led.conn).get(resolved)

    kind = current["kind"] if current else None

    def card(k: SourceKind, inner: str) -> str:
        chosen = kind == str(k)
        return f"""
      <div class="card" style="{'border-color:var(--brand);' if chosen else ''}">
        <div class="row" style="align-items:flex-start;gap:16px">
          <div>
            <h2>{views.esc(KIND_LABEL[k])}
              {'<span class="pill brand">in use</span>' if chosen else ''}</h2>
            <p class="sub" style="margin:3px 0 12px">{views.esc(KIND_BLURB[k])}</p>
            {inner}
          </div>
        </div>
      </div>"""

    from merchant.vault import posture

    security = posture()
    if security["encrypted_at_rest"]:
        storage_note = (
            "<b>The secret is encrypted at rest</b> with a key held outside "
            "the database, so a copy of the file is not a copy of the "
            "credential. Only the public key id is stored in the clear.")
    else:
        storage_note = (
            "<b>Test-mode keys only.</b> No encryption key is configured "
            "(LEDGERLINE_SECRET_KEY), so the secret is used to verify the "
            "connection and then dropped &mdash; never written to disk. Each "
            "sync will ask for it again.")

    razorpay_inner = f"""
    <form method="post" action="/sources/razorpay">
      <div class="row">
        <div><label>Key ID</label>
          <input name="key_id" placeholder="rzp_test_..."
            value="{views.esc(current['razorpay_key_id'] if current and
                              current['razorpay_key_id'] else '')}" required></div>
        <div><label>Key secret</label>
          <input name="key_secret" type="password" required></div>
        <div style="flex:0"><button>Connect</button></div>
      </div>
    </form>
    <p class="sub" style="margin:12px 0 0;font-size:12px">{storage_note}</p>
    <p class="sub" style="margin:8px 0 0;font-size:12px">
      Razorpay test mode does not settle, so a test connection will usually
      find zero settlements. The connector is real; there is simply nothing
      behind it until this points at a live account.</p>"""

    simulator_inner = """
    <form method="post" action="/sources/simulator">
      <button class="ghost">Use the simulator</button>
    </form>
    <p class="sub" style="margin:12px 0 0;font-size:12px">
      Adds a Simulator page where you record sales and settle them, so the
      auditor has real settlement lines to work on. Everything downstream is
      identical either way &mdash; the auditor cannot tell the difference, and
      that is the point.</p>"""

    status = ""
    if current and current["last_message"]:
        good = current["last_status"] == "ok"
        status = (f'<div class="banner {"brand" if good else "warn"}">'
                  f'<b>{"Connected" if good else "Problem"}</b>'
                  f'<span>{views.esc(current["last_message"])}</span></div>')

    banner = ""
    if error:
        banner = f'<div class="banner warn"><b>Could not connect</b><span>{views.esc(error)}</span></div>'
    elif ok:
        banner = f'<div class="banner brand"><b>Done</b><span>{views.esc(ok)}</span></div>'

    sync = ""
    if kind == str(SourceKind.RAZORPAY):
        with ledger(resolved) as led:
            has_secret = Sources(led.conn).stored_secret(resolved) is not None

        field = ('<div><label>Key secret</label>'
                 '<input name="key_secret" type="password" required></div>'
                 if not has_secret else
                 '<div class="sub" style="margin:0">The secret is stored '
                 'encrypted, so this can run unattended.</div>')
        forget = ('<form method="post" action="/sources/forget" '
                  'style="display:inline;margin-left:9px">'
                  '<button class="ghost">Forget the stored secret</button>'
                  '</form>' if has_secret else "")

        sync = f"""
      <div class="card">
        <h2>Pull settlements</h2>
        <p class="sub" style="margin:3px 0 12px">Reads the settlement recon
           report &mdash; the only place a gateway states, line by line, what it
           deducted and why. Our whole schema mirrors its columns.</p>
        <form method="post" action="/sources/sync">
          <div class="row">
            {field}
            <div style="flex:0"><button>Sync</button>{forget}</div>
          </div>
        </form>
      </div>"""

    body = f"""
{banner}{status}
<h1>Data source</h1>
<p class="sub">This platform is not where sales happen &mdash; it is where they
   get checked afterwards. Choose where its settlements come from.</p>

{card(SourceKind.RAZORPAY, razorpay_inner)}
{sync}
{card(SourceKind.SIMULATOR, simulator_inner)}

<div class="card">
  <h2>What is protected, and what is not</h2>
  <div class="money" style="margin-bottom:11px">
    <div class="lbl">Secrets encrypted at rest</div>
    <div class="val">{'<span class="pill good">yes</span>'
      if security["encrypted_at_rest"] else '<span class="pill">not configured</span>'}</div>
    <div class="lbl">Live Razorpay keys</div>
    <div class="val">{'<span class="pill warn">allowed</span>'
      if security["live_keys"] else '<span class="pill">refused</span>'}</div>
  </div>
  {''.join(f'<p class="sub" style="margin:0 0 4px;font-size:11.5px">'
           f'&middot; still missing: {views.esc(m)}</p>'
           for m in security["missing"])}
  <p class="sub" style="margin:10px 0 0;font-size:11.5px">Encryption at rest is
     one control, not a security posture. These are listed rather than glossed
     because an install that looks safe and is not is worse than one that
     admits what it lacks.</p>
</div>"""
    return views.page("Data source", body, "data", **shell)


@app.post("/sources/simulator")
def use_simulator(ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id
    ws.require_owner("the data source connection")
    from merchant.sources import Sources

    with ledger(resolved) as led:
        Sources(led.conn).use_simulator(resolved)
    return RedirectResponse("/data/simulator", status_code=303)


@app.post("/sources/forget")
def forget_secret(ws: Workspace = Depends(required_workspace)):
    ws.require_owner("the data source connection")
    from merchant.sources import Sources

    with ledger(ws.business_id) as led:
        Sources(led.conn).forget_secret(ws.business_id)
    return RedirectResponse("/data?ok=Stored+secret+deleted.",
                            status_code=303)


@app.post("/sources/razorpay")
def connect_razorpay(key_id: str = Form(...), key_secret: str = Form(...),
                     ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id
    ws.require_owner("the data source connection")

    from urllib.parse import quote

    from merchant.sources import Sources

    with ledger(resolved) as led:
        result = Sources(led.conn).connect_razorpay(resolved, key_id.strip(),
                                                    key_secret.strip())
    if not result.ok:
        return RedirectResponse(f"/data?error={quote(result.message)}",
                                status_code=303)
    return RedirectResponse(f"/data?ok={quote(result.message)}",
                            status_code=303)


@app.post("/sources/sync")
def sync_razorpay(key_secret: str = Form(""),
                  ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id
    ws.require_owner("the data source connection")

    from datetime import datetime, timezone
    from urllib.parse import quote

    from merchant.sources import Razorpay, Sources

    with ledger(resolved) as led:
        sources = Sources(led.conn)
        row = sources.get(resolved)
        if row is None or not row["razorpay_key_id"]:
            return RedirectResponse("/data", status_code=303)
        # A stored secret if there is one, otherwise whatever was typed. The
        # form field is optional precisely so a connection with an encrypted
        # secret does not have to ask.
        secret = key_secret.strip() or sources.stored_secret(resolved)
        if not secret:
            return RedirectResponse(
                f"/data?error={quote('No stored secret. Enter it to sync.')}",
                status_code=303)
        try:
            client = Razorpay(row["razorpay_key_id"], secret)
        except ValueError as exc:
            return RedirectResponse(f"/data?error={quote(str(exc))}",
                                    status_code=303)
        now = datetime.now(timezone.utc)
        result = client.settlements(now.year, now.month)
        sources.record_sync(resolved, result)

    key = "ok" if result.ok else "error"
    return RedirectResponse(f"/data?{key}={quote(result.message)}",
                            status_code=303)


@app.post("/sale")
def record_sale(ws: Workspace = Depends(required_workspace),
                rupees_: str = Form(alias="rupees"),
                description: str = Form(""),
                instrument: str = Form("upi")):
    resolved = ws.business_id

    try:
        paise = int(round(float(rupees_) * 100))
    except (ValueError, TypeError):
        return HTMLResponse(views.error_page(
            "That amount is not a number",
            f"'{rupees_}' could not be read as an amount.", "Back", "/data/simulator"),
            status_code=400)
    if paise <= 0:
        return HTMLResponse(views.error_page(
            "That amount is not positive",
            "A sale has to be for more than zero.", "Back", "/data/simulator"),
            status_code=400)

    method, network, card_type, intl = INSTRUMENTS.get(instrument,
                                                       INSTRUMENTS["upi"])
    with ledger(resolved) as led:
        order_id = led.create_order(paise, description or "Sale")
        led.capture_payment(order_id, method, network, card_type, intl)
    return RedirectResponse("/data/simulator", status_code=303)


@app.post("/refund")
def refund(payment_id: str = Form(...),
           ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id
    if resolved:
        with ledger(resolved) as led:
            led.refund_payment(payment_id)
    return RedirectResponse("/data/simulator", status_code=303)


# --- agents ---------------------------------------------------------------

@app.post("/agents/{agent_id}/toggle")
def toggle_agent(agent_id: str, ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id

    spec = catalog.get(agent_id)
    if spec is None or not spec.is_live:
        # A planned agent has no implementation. Letting it be switched on would
        # put a control in front of a user that does nothing.
        return HTMLResponse(views.error_page(
            "That agent is not built yet",
            "It is on the roadmap. Turning it on would do nothing.",
            "Back to agents", "/agents"), status_code=400)

    with ledger(resolved) as led:
        on = led.businesses.agent_enabled(resolved, agent_id)
        led.businesses.set_agent(resolved, agent_id, not on)
    return RedirectResponse("/agents", status_code=303)


# --- pulling purchases out of Zoho Books -----------------------------------
#
# Plumbing, and worth labelling as such. It moves rows from one system to
# another and judges none of them. "We integrate with your accounting software"
# is the kind of claim that gets mistaken for intelligence, and the
# intelligence in this product is elsewhere.


@app.get("/data/zoho", response_class=HTMLResponse)
def zoho_page(request: Request, ws: Workspace = Depends(required_workspace),
              error: str = "", ok: str = ""):
    from merchant.vault import Vault
    from merchant.zoho import REGIONS, ZohoConnections

    ws.require_owner("how this business gets its purchase data", request)

    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        connection = ZohoConnections(led.conn).get(ws.business_id)

    have_vault = Vault.from_env() is not None

    banner = ""
    if error:
        banner = '<div class="banner warn"><span>' + views.esc(error) + "</span></div>"
    elif ok:
        banner = '<div class="banner brand"><span>' + views.esc(ok) + "</span></div>"
    if not have_vault:
        banner += (
            '<div class="banner warn">No encryption key is configured, so '
            'there is nowhere safe to keep the Zoho credentials and the '
            'connection is refused. Generate one with '
            '<code>python -m merchant.vault</code> and set '
            'LEDGERLINE_SECRET_KEY.</div>')

    if connection is not None and connection["refresh_token_encrypted"]:
        body = f"""
{banner}
<h1>Zoho Books</h1>
<p class="sub">Purchase bills come straight from your books.</p>

<div class="card">
  <div class="row" style="align-items:center">
    <div>
      <h2 style="margin:0">Connected to
        {views.esc(connection["organization_name"] or connection["organization_id"])}</h2>
      <p class="sub" style="margin:3px 0 0">
        {views.esc(connection["last_pull_note"] or "Nothing pulled yet.")}
        {f'Last pull {views.when(connection["last_pull_at"])}.'
         if connection["last_pull_at"] else ''}
      </p>
    </div>
    <div style="flex:0">
      <form method="post" action="/zoho/import" style="display:inline">
        <button>Pull purchase bills</button></form>
      <form method="post" action="/zoho/disconnect" style="display:inline">
        <button class="ghost small">Disconnect</button></form>
    </div>
  </div>
  <p class="sub" style="margin:12px 0 0;font-size:11.5px">
    Read-only access: contacts, bills and settings. The connection cannot
    create, edit or delete anything in your books, and that is enforced by the
    scope Zoho granted rather than by our intent.
  </p>
</div>"""
        return views.page("Zoho Books", body, "data", **shell)

    region_options = "".join(
        f'<option value="{r}"{" selected" if r == "in" else ""}>{r}</option>'
        for r in REGIONS)
    redirect_uri = str(request.base_url).rstrip("/") + "/zoho/callback"

    body = f"""
{banner}
<h1>Connect Zoho Books</h1>
<p class="sub">Pull purchase bills straight out of your books instead of
   typing them in.</p>

<div class="card">
  <h2>What you need first</h2>
  <p class="sub" style="margin:3px 0 12px">Create a Self Client at
     <b>api-console.zoho.in</b>. It gives you a Client ID and a Client Secret.
     Your Organization ID is in Zoho Books under Settings. Set the redirect URI
     to exactly:</p>
  <div class="mono" style="padding:9px 12px;background:var(--raised);
    border:1px solid var(--line-2);border-radius:7px;font-size:12px">
    {views.esc(redirect_uri)}</div>
</div>

<div class="card">
  <h2>Connect</h2>
  <form method="post" action="/zoho/begin">
    <div class="row">
      <div><label>Client ID</label>
        <input name="client_id" placeholder="1000.XXXXXXXX" required></div>
      <div><label>Client Secret</label>
        <input name="client_secret" type="password" required></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div><label>Organization ID</label>
        <input name="organization_id" placeholder="60000000000" required></div>
      <div><label>Data centre</label>
        <select name="region">{region_options}</select></div>
      <div style="flex:0;align-self:flex-end">
        <button>Continue to Zoho</button></div>
    </div>
    <p class="sub" style="margin:12px 0 0;font-size:11.5px">
      You will be sent to Zoho to approve the access. Your Zoho password is
      entered on Zoho&rsquo;s own site and never reaches this platform. The
      secret above is encrypted before it is stored, and if no encryption key
      is configured it is not stored at all.
    </p>
  </form>
</div>"""
    return views.page("Connect Zoho Books", body, "data", **shell)


@app.post("/zoho/begin")
def zoho_begin(request: Request, client_id: str = Form(...),
               client_secret: str = Form(...),
               organization_id: str = Form(...), region: str = Form("in"),
               ws: Workspace = Depends(required_workspace)):
    from urllib.parse import quote

    from merchant.zoho import ZohoConnections, authorise_url

    ws.require_owner("how this business gets its purchase data", request)

    with ledger(ws.business_id) as led:
        state = ZohoConnections(led.conn).begin(
            ws.business_id, client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            organization_id=organization_id.strip(), region=region)

    if state is None:
        return RedirectResponse(
            "/data/zoho?error=" + quote("There is nowhere safe to store the Zoho "
                                   "secret, so the connection was refused."),
            status_code=303)

    redirect_uri = str(request.base_url).rstrip("/") + "/zoho/callback"
    return RedirectResponse(
        authorise_url(client_id.strip(), redirect_uri, region, state),
        status_code=303)


@app.get("/zoho/callback")
def zoho_callback(request: Request, code: str = "", state: str = "",
                  error: str = "", ws: Workspace = Depends(required_workspace)):
    """
    Zoho sends the merchant back here with a one-time code.

    The state token is checked against the one minted when the flow started.
    Without that check, anyone could send a logged-in owner to this URL with a
    code from THEIR Zoho account and have this business quietly start reading
    somebody else's books.
    """
    from urllib.parse import quote

    from merchant.zoho import REGIONS, ZohoConnections
    from merchant.vault import Vault

    if error or not code:
        return RedirectResponse(
            "/data/zoho?error=" + quote(error or "Zoho sent no authorisation code."),
            status_code=303)

    with ledger(ws.business_id) as led:
        connections = ZohoConnections(led.conn)
        row = connections.get(ws.business_id)
        if row is None or not row["state_token"]:
            return RedirectResponse(
                "/data/zoho?error=" + quote("There is no connection in progress."),
                status_code=303)
        if not secrets.compare_digest(row["state_token"], state or ""):
            AccessLog(led.conn).denied(
                Action.CONNECT_SOURCE, user=ws.user,
                business_id=ws.business_id,
                detail="Zoho callback state did not match")
            return RedirectResponse(
                "/data/zoho?error=" + quote("That authorisation did not come from "
                                       "the request this business started."),
                status_code=303)

        vault = Vault.from_env()
        client_secret = vault.decrypt(row["client_secret_encrypted"] or "") \
            if vault else None
        if not client_secret:
            return RedirectResponse(
                "/data/zoho?error=" + quote("The stored client secret could not be "
                                       "read. Start again."),
                status_code=303)

        accounts, _api = REGIONS.get(row["region"], REGIONS["in"])
        redirect_uri = str(request.base_url).rstrip("/") + "/zoho/callback"
        try:
            import httpx

            with httpx.Client(timeout=20) as http:
                response = http.post(
                    f"{accounts}/oauth/v2/token",
                    data={"code": code, "client_id": row["client_id"],
                          "client_secret": client_secret,
                          "redirect_uri": redirect_uri,
                          "grant_type": "authorization_code"})
            payload = response.json()
        except Exception as exc:                            # noqa: BLE001
            return RedirectResponse(
                "/data/zoho?error=" + quote(f"Could not reach Zoho: {exc}"),
                status_code=303)

        refresh = payload.get("refresh_token")
        if not refresh:
            return RedirectResponse(
                "/data/zoho?error=" + quote(
                    f"Zoho returned no refresh token "
                    f"({payload.get('error', 'no reason given')}). The Self "
                    f"Client may not have offline access enabled."),
                status_code=303)

        connections.complete(ws.business_id, refresh)
        client = connections.client(ws.business_id)
        checked = client.check() if client else None
        if checked is not None and checked.ok:
            connections.complete(ws.business_id, refresh,
                                 checked.message.replace("Connected to ", "")
                                 .rstrip("."))
        AccessLog(led.conn).record(
            Action.CONNECT_SOURCE, user=ws.user, business_id=ws.business_id,
            detail="connected Zoho Books")

    return RedirectResponse("/data/zoho?ok=Connected.", status_code=303)


@app.post("/zoho/import")
def zoho_import(request: Request, ws: Workspace = Depends(required_workspace)):
    """
    Pull bills and record them as purchases.

    Every bill that cannot be reconciled is SKIPPED and named, rather than
    imported and left permanently showing as unfiled - a missing GSTIN is
    missing data, and showing it as a supplier default would be a false
    accusation manufactured by our own importer.
    """
    from urllib.parse import quote

    from merchant.zoho import ZohoConnections, ZohoError, importable, to_purchase

    ws.require_owner("how this business gets its purchase data", request)

    with ledger(ws.business_id) as led:
        connections = ZohoConnections(led.conn)
        client = connections.client(ws.business_id)
        if client is None:
            return RedirectResponse(
                "/data/zoho?error=" + quote("Zoho is not connected."),
                status_code=303)

        try:
            vendors = {str(v.get("contact_id")): v for v in client.vendors()}
            bills = client.bills()
        except ZohoError as exc:
            return RedirectResponse("/data/zoho?error=" + quote(str(exc)),
                                    status_code=303)

        existing = {r["invoice_number"] for r in led.purchases(limit=5_000)}
        imported, skipped = 0, []
        for bill in bills:
            purchase = to_purchase(bill, vendors)
            reason = importable(purchase)
            if reason:
                skipped.append(f"{purchase['invoice_number'] or bill.get('bill_id')}: {reason}")
                continue
            if purchase["invoice_number"] in existing:
                continue
            led.record_zoho_purchase(purchase)
            imported += 1

        note = (f"Pulled {len(bills)} bills from {len(vendors)} vendors, "
                f"imported {imported}"
                + (f", skipped {len(skipped)}" if skipped else "") + ".")
        connections.record_pull(ws.business_id, note)

    message = note
    if skipped:
        message += " Skipped: " + "; ".join(skipped[:4])
        if len(skipped) > 4:
            message += f" and {len(skipped) - 4} more"
    return RedirectResponse("/data/zoho?ok=" + quote(message), status_code=303)


@app.post("/zoho/disconnect")
def zoho_disconnect(request: Request,
                    ws: Workspace = Depends(required_workspace)):
    from merchant.zoho import ZohoConnections

    ws.require_owner("how this business gets its purchase data", request)
    with ledger(ws.business_id) as led:
        ZohoConnections(led.conn).disconnect(ws.business_id)
        AccessLog(led.conn).record(
            Action.CONNECT_SOURCE, user=ws.user, business_id=ws.business_id,
            detail="disconnected Zoho Books")
    return RedirectResponse("/data/zoho?ok=Disconnected.", status_code=303)


# --- supplier risk ----------------------------------------------------------
#
# The reconciliation answers "did this month's invoices reach GSTR-2B". This
# answers the question underneath it: given how these suppliers have behaved
# for three years, how much of the credit you are about to claim is actually
# going to arrive.
#
# The dangerous case is invisible to a reconciliation. A supplier who files
# GSTR-1 punctually and never files GSTR-3B puts the invoice in your GSTR-2B -
# so everything matches - while the tax was never paid, and under s.16(2)(c)
# the credit does not exist.

RISK_RUNS: dict = {}
_risk_lock = threading.Lock()

TRUST_TONE = [(75, ui.TONE_GOOD), (50, ui.TONE_WARN), (0, ui.TONE_BAD)]


def _trust_tone(score: int) -> str:
    for floor, tone in TRUST_TONE:
        if score >= floor:
            return tone
    return ui.TONE_BAD


def _run_risk(key: str, business_id: str, data: bytes, filename: str,
              use_agent: bool) -> None:
    from merchant.purchase_import import parse
    from merchant.risk_pipeline import NO_HISTORY, history_service_for
    from merchant.risk_pipeline import run as run_pipeline

    def progress(**kw):
        with _risk_lock:
            state = RISK_RUNS.get(key)
            if state is not None:
                state.update(kw)

    try:
        imported = parse(data, filename)
        if imported.ok:
            # The same file feeds both halves of this agent. Risk analysis
            # reads it; reconciliation needs it stored. Parsing it twice - once
            # here and once on another screen - would be two chances for a
            # merchant's register to mean two different things.
            with ledger(business_id) as led:
                led.replace_purchase_register(imported)
        if not imported.ok:
            raise ValueError(
                "No supplier rows could be read. "
                + (f"Missing columns: {', '.join(imported.missing_columns)}."
                   if imported.missing_columns else
                   "Every row was skipped - check the GSTIN column."))
        # Which of the three sources this business has is decided once, here,
        # and every supplier in the run is read the same way. Never a blend -
        # half a table from real filings and half from personas, with nothing
        # saying which, is worse than either alone.
        with ledger(business_id) as led:
            history = history_service_for(led, business_id)
        if history is None:
            # Live data with nothing to score against. Refusing is the answer;
            # simulating would put invented filing records against real
            # companies and present it as a risk assessment.
            raise ValueError(NO_HISTORY)
        portfolio = run_pipeline(imported, use_agent=use_agent,
                                 history=history, on_progress=progress)
        with _risk_lock:
            RISK_RUNS[key] = {"state": "done", "phase": "done",
                              "payload": portfolio.as_dict()}
    except Exception as exc:                                # noqa: BLE001
        with _risk_lock:
            RISK_RUNS[key] = {"state": "failed",
                              "phase": f"{type(exc).__name__}: {exc}"}


def _risk_mode(led, business_id: str) -> tuple[str, dict, dict]:
    """
    Which of the three screens this business gets, and what each needs.

    The mode is decided by what the business is CONNECTED to, not by what
    happens to be uploaded. A merchant on the simulator is in demo mode whether
    or not a file was once dropped on the old screen, and a merchant on live
    data does not get a demo button. Anything else and the page would change
    shape under them for reasons they cannot see.
    """
    from merchant.sources import SourceKind, Sources

    sources = Sources(led.conn)
    kind = sources.kind(business_id)
    api_config = sources.filing_api_config(business_id)
    summary = led.filing_history_summary()

    # No source chosen yet is treated as the simulator: it is what a new
    # business is actually running on, and offering the demo is a better first
    # screen than an upload box for data the platform cannot yet do anything
    # with.
    if kind is None or kind == SourceKind.SIMULATOR:
        return views.MODE_DEMO, api_config or {}, summary
    if api_config and api_config.get("key_available"):
        return views.MODE_LIVE_API, api_config, summary
    return views.MODE_LIVE_MANUAL, api_config or {}, summary


def _risk_screen(mode: str, api_config: dict, summary: dict) -> str:
    """The upload screen for a mode. One place, so the states cannot drift."""
    if mode == views.MODE_DEMO:
        return views.risk_demo_screen()
    if mode == views.MODE_LIVE_API:
        return views.risk_live_api_screen(api_config.get("message", ""))
    return views.risk_live_manual_screen(summary)


@app.get("/agents/input-credit", response_class=HTMLResponse)
def supplier_risk_page(ws: Workspace = Depends(required_workspace),
                       key: str = "", error: str = "", ok: str = ""):
    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        head = _workspace_head(led, ws, "gst_itc", "")
        mode, api_config, summary = _risk_mode(led, ws.business_id)

    screen = _risk_screen(mode, api_config, summary)

    with _risk_lock:
        state = dict(RISK_RUNS.get(key) or {}) if key else {}

    banner = ""
    if error:
        banner = f'<div class="banner warn"><span>{views.esc(error)}</span></div>'
    elif ok:
        banner = f'<div class="banner brand"><span>{views.esc(ok)}</span></div>'

    if state.get("state") == "running":
        return HTMLResponse(_risk_running(state, head, shell))
    if state.get("state") == "failed":
        banner = (f'<div class="banner warn"><span>'
                  f'{views.esc(state.get("phase", "It failed."))}</span></div>')

    payload = state.get("payload")
    if not payload:
        return HTMLResponse(views.page(
            "Supplier risk", head + banner + screen, "agent:gst_itc",
            **shell))

    return HTMLResponse(views.page(
        "Supplier risk", head + banner + _risk_results(payload, key),
        "agent:gst_itc", **shell))


def _risk_running(state: dict, head: str, shell: dict) -> str:
    done, total = state.get("done", 0), state.get("total", 0)
    bar = ""
    if total:
        pct = int(100 * done / total)
        bar = (f'<div style="height:6px;background:var(--line-2);'
               f'border-radius:3px;overflow:hidden;margin-top:12px">'
               f'<div style="height:100%;width:{pct}%;background:var(--brand)">'
               f'</div></div>')
    body = f"""
{head}
<div class="card">
  <div style="display:flex;align-items:center;gap:11px">
    <span class="spinner"></span>
    <div>
      <div style="font-weight:580">Working through your suppliers</div>
      <div class="sub" style="margin-top:2px">
        {views.esc(state.get("phase", "Starting"))}</div>
    </div>
  </div>
  {bar}
</div>
<meta http-equiv="refresh" content="1">"""
    return views.page("Supplier risk", body, "agent:gst_itc", **shell)


# --- the supplier drawer ----------------------------------------------------
#
# Rendered server-side, one per supplier, hidden until a row is clicked. No
# round trip and no client-side state: everything a drawer shows was computed
# by the pipeline, so opening one is a CSS class change rather than a fetch and
# a re-render. It also means the drawer cannot show something the table
# disagrees with - they are the same data, printed twice.


def _grid(cells: list[dict]) -> str:
    """Thirty-six months, twelve to a row, oldest first."""
    if not cells:
        return '<p class="sub" style="margin:0">No filing history.</p>'
    squares = "".join(
        f'<i class="g-{c["status"]}" title="{views.esc(c["period"])} '
        f'&mdash; {views.esc(c["label"])}"></i>' for c in cells)
    first, last = cells[0]["period"], cells[-1]["period"]
    return f"""
<div class="grid36">{squares}</div>
<div class="grid-years"><span>{views.esc(first)}</span>
  <span>{views.esc(last)}</span></div>
<div class="grid-key">
  <span><i class="g-on_time"></i>Filed on time</span>
  <span><i class="g-late"></i>Filed late</span>
  <span><i class="g-missed"></i>Reported, never paid</span>
  <span><i class="g-silent"></i>Filed nothing</span>
</div>"""


def _clocks(clocks: dict) -> str:
    """
    The two deadlines that decide whether credit survives.

    Both figures arrive already computed. A countdown worked out in the browser
    would be a second implementation of a statutory rule, free to disagree with
    the one the findings were built from.
    """
    if not clocks or not clocks.get("invoices"):
        return ""

    r37_days = clocks.get("rule_37_days_left")
    breached = clocks.get("rule_37_breached_count", 0)
    if breached:
        r37_tone, r37_big = "bad", f"{breached} overdue"
        r37_what = (f"{rupees(clocks['rule_37_breached_tax'])} of credit must "
                    f"be reversed unless these are paid. Rule 37 gives "
                    f"{clocks['window_days']} days from the invoice.")
    elif r37_days is not None and r37_days <= 30:
        r37_tone, r37_big = "warn", f"{r37_days} days"
        r37_what = ("until the soonest invoice hits 180 days unpaid. After "
                    "that the credit on it has to be given back.")
    else:
        r37_tone, r37_big = "", f"{r37_days} days"
        r37_what = (f"until the soonest of these invoices reaches "
                    f"{clocks['window_days']} days unpaid.")

    claim_days = clocks.get("claim_days_left")
    expired = clocks.get("claim_expired_count", 0)
    if expired:
        c_tone, c_big = "bad", f"{expired} expired"
        c_what = (f"{rupees(clocks['claim_expired_tax'])} is past its s.16(4) "
                  f"deadline and cannot be claimed at all.")
    elif claim_days is not None and claim_days <= 90:
        c_tone, c_big = "warn", f"{claim_days} days"
        c_what = ("until the soonest of these can no longer be claimed under "
                  "s.16(4). After that it is gone.")
    else:
        c_tone, c_big = "", f"{claim_days} days"
        c_what = "until the soonest s.16(4) claim deadline."

    return f"""
<div class="clocks">
  <div class="clock {r37_tone}">
    <div class="rule">Rule 37 &middot; pay within {clocks['window_days']} days</div>
    <b>{views.esc(r37_big)}</b>
    <div class="what">{r37_what}</div>
  </div>
  <div class="clock {c_tone}">
    <div class="rule">Section 16(4) &middot; claim deadline</div>
    <b>{views.esc(c_big)}</b>
    <div class="what">{c_what}</div>
  </div>
</div>"""


def _drawer(sup: dict, index: int, patterns: dict, actions: dict) -> str:
    """One supplier's drawer, hidden until its row is clicked."""
    prof = sup.get("profile") or {}
    clocks = sup.get("clocks") or {}

    invoice_rows = "".join(f"""
      <tr>
        <td class="mono">{views.esc(i.get("invoice_number", ""))}</td>
        <td class="r">{views.esc(i.get("invoice_date", ""))}</td>
        <td class="r">{rupees(i.get("total_tax", 0))}</td>
        <td class="r" style="color:{'var(--danger)' if i.get('rule_37_breached')
                                    else 'var(--muted)'}">
          {i.get("rule_37_days_left", 0)}d</td>
        <td class="r" style="color:{'var(--danger)' if i.get('claim_expired')
                                    else 'var(--muted)'}">
          {i.get("claim_days_left", 0)}d</td>
      </tr>""" for i in clocks.get("invoices", []))

    from engine.gst.filing_history import SOURCE_LABEL

    facts = "".join(
        f'<div class="working-line"><span>{label}</span><b>{value}</b></div>'
        for label, value in [
            ("Filing record from",
             SOURCE_LABEL.get(sup.get("history_source", ""),
                              sup.get("history_source", "") or "unknown")),
            ("Periods of history", prof.get("periods", 0)),
            ("Reported sales in",
             f'{prof.get("gstr1_filed", 0)} ({prof.get("coverage_pct", 0)}%)'),
            ("Paid the tax in",
             f'{prof.get("gstr3b_filed", 0)} '
             f'({prof.get("compliance_pct", 0)}% of what they reported)'),
            ("Reported and did not pay",
             f'{prof.get("sold_but_did_not_pay", 0)} times '
             f'({prof.get("default_rate_pct", 0)}%)'),
            ("Same, last 12 months", f'{prof.get("recent_default_rate_pct", 0)}%'),
            ("Average GSTR-3B delay",
             f'{prof.get("avg_gstr3b_delay_days", 0)} days '
             f'(worst {prof.get("worst_gstr3b_delay_days", 0)})'),
            ("Registration", prof.get("registration_status", "unknown")),
        ])

    return f"""
<div class="drawer" id="dr-{index}" role="dialog" aria-modal="true"
     aria-label="{views.esc(sup["supplier_name"])}">
  <div class="drawer-head">
    <div>
      <h2>{views.esc(sup["supplier_name"])}</h2>
      <div class="mono" style="color:var(--muted);font-size:11px;margin-top:2px">
        {views.esc(sup["gstin"])}</div>
    </div>
    <button class="drawer-close" data-close aria-label="Close">&times;</button>
  </div>
  <div class="drawer-body">
    <h3>What we recommend</h3>
    <div class="recommend" style="margin:0 0 9px">
      <span class="recommend-label">Recommended</span>
      {views.esc(actions.get(sup["action"], sup["action"]))}
    </div>
    <p class="sub" style="margin:0">{views.esc(sup["reasoning"])}</p>
    {f'''<p style="margin:9px 0 0;font-size:12.2px;color:var(--warn)">
      Reading the same record, the agent would have said
      <b>{views.esc(actions.get(sup.get("agent_action", ""), sup.get("agent_action", "")))}</b>.
      The recommendation above comes from the figures rather than from the
      agent, so it does not change between runs &mdash; but where the agent is
      more cautious than the rule, that is worth knowing.</p>'''
      if sup.get("goes_further") else ''}
    {f'<p style="margin:9px 0 0;font-size:12.4px;color:var(--warn)">Watch for: {views.esc(sup["watch_for"])}</p>' if sup.get("watch_for") else ''}

    <h3>Statutory clocks</h3>
    {_clocks(clocks)}

    <h3>36 months of filing</h3>
    {'''<p class="sub" style="margin:0">The active source has no record of
       this supplier, so nothing is claimed about their filing either way.
       They are scored as unknown rather than as clean &mdash; which is why
       the recommendation above is to watch rather than to trust.</p>'''
     if not sup.get("history_known", True)
     else _grid(sup.get("compliance_grid", []))}

    <h3>Their record</h3>
    <div class="working-body" style="margin:0">{facts}</div>

    <h3>This month from them</h3>
    <div style="overflow-x:auto">
      <table>
        <tr><th>Invoice</th><th class="r">Date</th><th class="r">Tax</th>
            <th class="r">Rule 37</th><th class="r">s.16(4)</th></tr>
        {invoice_rows}
      </table>
    </div>

    <h3>Draft a document</h3>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <form method="post" action="/agents/input-credit/notice" style="flex:0">
        <input type="hidden" name="gstin" value="{views.esc(sup["gstin"])}">
        <input type="hidden" name="key" value="{{key}}">
        <button class="ghost small">Draft vendor notice</button>
      </form>
      <form method="post" action="/agents/input-credit/defence" style="flex:0">
        <input type="hidden" name="gstin" value="{views.esc(sup["gstin"])}">
        <input type="hidden" name="key" value="{{key}}">
        <button class="ghost small">Generate DRC-01C defence</button>
      </form>
    </div>
    <p class="sub" style="margin:9px 0 0;font-size:11.3px">
      Both are built from this supplier&rsquo;s invoices above and nothing
      else. Nothing is sent &mdash; you get the text to read, edit and send
      yourself.
    </p>
  </div>
</div>"""


DRAWER_SCRIPT = """
<div class="drawer-back" id="dr-back"></div>
<script>
(function () {
  var back = document.getElementById('dr-back');
  function shut() {
    document.querySelectorAll('.drawer.open').forEach(function (d) {
      d.classList.remove('open');
    });
    back.classList.remove('open');
    document.body.style.overflow = '';
  }
  document.querySelectorAll('tr.clickable').forEach(function (row) {
    row.addEventListener('click', function () {
      var drawer = document.getElementById(row.dataset.drawer);
      if (!drawer) { return; }
      shut();
      drawer.classList.add('open');
      back.classList.add('open');
      document.body.style.overflow = 'hidden';
      var close = drawer.querySelector('[data-close]');
      if (close) { close.focus(); }
    });
  });
  document.querySelectorAll('[data-close]').forEach(function (b) {
    b.addEventListener('click', shut);
  });
  back.addEventListener('click', shut);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { shut(); }
  });
})();
</script>"""


def _supplier_from_run(key: str, gstin: str) -> Optional[dict]:
    """
    One supplier out of a completed analysis.

    Bound to the run and the GSTIN together: a document is built from the data
    that was on screen when the button was pressed, not from whatever the
    latest upload happens to hold.
    """
    with _risk_lock:
        state = dict(RISK_RUNS.get(key) or {})
    payload = state.get("payload")
    if not payload:
        return None
    wanted = (gstin or "").strip().upper()
    return next((s for s in payload["suppliers"]
                 if s["gstin"].upper() == wanted), None)


def _document_page(document, shell, head, note: str = "") -> HTMLResponse:
    body = f"""
{head}
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing has been sent.</b> This is text for you to read, edit and
  send yourself.</span>
</div>
{f'<div class="banner warn"><span>{views.esc(note)}</span></div>' if note else ''}

<div class="card">
  <div class="row" style="align-items:flex-start">
    <div>
      <h2 style="margin:0">{views.esc(document.title)}</h2>
      <p class="sub" style="margin:3px 0 0">
        {len(document.invoices)} invoice{'' if len(document.invoices) == 1 else 's'}
        &middot; {rupees(document.amount)}
        &middot; {'the agent wrote the argument' if document.written_by == 'agent'
                  else 'assembled from the record'}</p>
    </div>
    <div style="flex:0">
      <a class="btn ghost small" href="/agents/input-credit">Back</a></div>
  </div>
  <div class="draft" style="margin-top:14px;font-family:ui-monospace,
    SFMono-Regular,Menlo,monospace;font-size:12.2px;line-height:1.55">
{views.esc(document.body)}</div>
</div>

<div class="card tint">
  <h2>What is in it and what is not</h2>
  <p class="sub" style="margin:4px 0 0">Every invoice number, date, amount and
     total above was assembled from this supplier&rsquo;s own rows &mdash;
     never written by a model. The paragraph making the case was, and it is
     checked for figures that appear in no input before you see it.</p>
</div>"""
    return HTMLResponse(views.page(document.title, body, "agent:gst_itc",
                                   **shell))


@app.post("/agents/input-credit/notice")
def draft_vendor_notice(key: str = Form(""), gstin: str = Form(""),
                        ws: Workspace = Depends(required_workspace)):
    """A formal notice to the supplier, citing s.16(2)(c)."""
    from agent.vendor_documents import vendor_notice, write_case

    supplier = _supplier_from_run(key, gstin)
    if supplier is None:
        return RedirectResponse(
            "/agents/input-credit?error=That+analysis+is+no+longer+in+memory."
            "+Upload+the+register+again.", status_code=303)

    case, error = write_case(supplier, "vendor_notice")
    document = vendor_notice(supplier, case)

    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        head = _workspace_head(led, ws, "gst_itc", "")
        AccessLog(led.conn).record(
            Action.VIEW_DISPUTE, user=ws.user, business_id=ws.business_id,
            target=supplier["gstin"], detail="drafted a vendor notice")

    note = ("" if not error else
            f"The agent could not write the argument ({error}), so the notice "
            f"below states the record without it. Every figure in it is still "
            f"exact.")
    return _document_page(document, shell, head, note)


@app.post("/agents/input-credit/defence")
def draft_drc01c_defence(key: str = Form(""), gstin: str = Form(""),
                         ws: Workspace = Depends(required_workspace)):
    """A reply to the automatic Rule 88D mismatch notice."""
    from agent.vendor_documents import drc01c_defence, write_case

    supplier = _supplier_from_run(key, gstin)
    if supplier is None:
        return RedirectResponse(
            "/agents/input-credit?error=That+analysis+is+no+longer+in+memory."
            "+Upload+the+register+again.", status_code=303)

    case, error = write_case(supplier, "drc01c_defence")
    document = drc01c_defence(supplier, case)

    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        head = _workspace_head(led, ws, "gst_itc", "")
        AccessLog(led.conn).record(
            Action.VIEW_DISPUTE, user=ws.user, business_id=ws.business_id,
            target=supplier["gstin"], detail="drafted a DRC-01C reply")

    note = ("" if not error else
            f"The agent could not write the argument ({error}), so the reply "
            f"below states the record and the circulars without it.")
    return _document_page(document, shell, head, note)


def _risk_results(payload: dict, key: str = "") -> str:
    """The portfolio, then the suppliers, worst first."""
    from engine.gst.risk import PATTERN_LABEL
    from agent.risk_agent import ACTION_LABEL

    port = payload["portfolio"]
    suppliers = payload["suppliers"]

    skipped = ""
    if port["rows_skipped"]:
        shown = "; ".join(port["rows_skipped"][:5])
        more = (f" and {len(port['rows_skipped']) - 5} more"
                if len(port["rows_skipped"]) > 5 else "")
        skipped = (f'<div class="banner warn"><span><b>'
                   f'{len(port["rows_skipped"])} rows were not read.</b> '
                   f'{views.esc(shown)}{more}</span></div>')

    failed = ""
    if port["failed_calls"]:
        failed = (f'<div class="banner warn"><span><b>'
                  f'{port["failed_calls"]} supplier'
                  f'{"" if port["failed_calls"] == 1 else "s"} could not be '
                  f'judged.</b> Those rows show the score computed from their '
                  f'record, without the agent on top.</span></div>')

    # Where these figures came from, on the results page as well as the upload
    # page. A trust score is read as fact; one computed from generated filing
    # dates has to carry that on the same screen, not one click away.
    provenance = ""
    if port.get("history_is_demo"):
        provenance = (
            '<div class="src demo" style="margin-bottom:16px">'
            '<span class="src-dot"></span><div>'
            '<b>These scores come from simulated filing history</b>'
            '<div class="src-what">No GST API is configured and no filing '
            'history has been uploaded for this business. The register and the '
            'arithmetic are real; the filing dates are generated. '
            '<b>Do not act on these against a real supplier.</b>'
            '</div></div></div>')
    elif port.get("history_source"):
        provenance = (
            f'<div class="src ok" style="margin-bottom:16px">'
            f'<span class="src-dot"></span><div>'
            f'<b>{views.esc(port["history_source_label"])}</b>'
            f'<div class="src-what">'
            f'{views.esc(port["history_source_note"])}</div></div></div>')

    unknown = ""
    if port.get("suppliers_without_history"):
        n = port["suppliers_without_history"]
        unknown = (
            f'<div class="banner warn"><span><b>{n} supplier'
            f'{"" if n == 1 else "s"} could not be found in the active '
            f'source.</b> They are scored as <i>unknown</i> and recommended '
            f'for watching &mdash; never assumed clean. Add them to your '
            f'filing history to score them properly.</span></div>')

    if port.get("history_failures"):
        shown = "; ".join(port["history_failures"][:3])
        more = (f" and {len(port['history_failures']) - 3} more"
                if len(port["history_failures"]) > 3 else "")
        unknown += (f'<div class="banner warn"><span><b>The filing API could '
                    f'not be read for {len(port["history_failures"])} '
                    f'supplier{"" if len(port["history_failures"]) == 1 else "s"}.'
                    f'</b> {views.esc(shown)}{more}</span></div>')

    rows = "".join(_risk_row(sup, PATTERN_LABEL, ACTION_LABEL, n)
                   for n, sup in enumerate(suppliers))
    drawers = "".join(
        _drawer(sup, n, PATTERN_LABEL, ACTION_LABEL).replace("{key}", key)
        for n, sup in enumerate(suppliers))

    # What this click cost. Shown because somebody is paying for it, and
    # because the cache share is the difference between rupees and hundreds of
    # them - a consequence of how the prompt is arranged, not of luck.
    usage = port.get("usage") or {}
    spend = ""
    if usage.get("calls"):
        spend = (
            f'<div style="padding:11px 16px;border-top:1px solid var(--line-2);'
            f'color:var(--muted);font-size:11.5px">'
            f'{usage["calls"]} agent call'
            f'{"" if usage["calls"] == 1 else "s"}, one per supplier &mdash; '
            f'<b>${usage["usd"]:.3f}</b> (about Rs {usage["rupees"]:.0f}). '
            f'{usage["cached_share_pct"]}% of the input was read from cache, '
            f'because the instructions are byte-identical on every supplier '
            f'and only the record changes.'
            f'</div>')

    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here is filed, claimed or paid.</b> Every recommendation is
  a proposal waiting for you.</span>
</div>
{provenance}
{skipped}
{failed}
{unknown}

<div class="card" style="padding:0;overflow:hidden;margin-bottom:16px">
  <div class="stats">
    <div class="stat"><b>{port["total_pending_itc_display"]}</b>
      <span>input credit pending</span></div>
    <div class="stat"><b style="color:var(--danger)">
      {port["itc_at_risk_display"]}</b>
      <span>their record puts at risk</span></div>
    <div class="stat"><b>{port["high_risk_suppliers"]}/{port["suppliers"]}</b>
      <span>suppliers needing attention</span></div>
    <div class="stat"><b>{port["rows_read"]}</b>
      <span>invoices read</span></div>
  </div>
</div>

<div class="card flush">
  <div class="card-head"><h2>Supplier risk</h2>
    <span class="sub">worst first</span></div>
  <div style="overflow-x:auto">
  <table>
    <tr><th>Supplier</th><th class="r">Credit this month</th>
        <th class="r">At risk</th><th class="r">Trust</th>
        <th>Record</th><th>Recommended</th></tr>
    {rows}
  </table>
  </div>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    The recommendation comes from the figures, not from the agent, so it does
    not change between runs. The agent reads the record, explains it, and says
    when it would go further. Click any supplier for their 36-month filing
    record, their statutory clocks and the documents you can send. Trust is a weighted summary of what
    a supplier has already done &mdash;
    whether they paid the tax they reported, whether they reported at all,
    whether it was on time, and whether their registration is alive. It is not
    a prediction, and the weights are in engine/gst/risk.py where they can be
    argued with.
  </div>
  {spend}
</div>

{drawers}
{DRAWER_SCRIPT}"""


def _risk_row(sup: dict, patterns: dict, actions: dict, index: int) -> str:
    """
    One row in the table. Clicking it opens that supplier's drawer.

    The row carries the summary and nothing else - everything a person might
    want to dig into lives in the drawer, so the table stays scannable at
    twenty suppliers instead of becoming twenty stacked accordions.
    """
    tone = _trust_tone(sup["trust_score"])
    return f"""
      <tr class="clickable" data-drawer="dr-{index}">
        <td>{views.esc(sup["supplier_name"])}
          <div class="mono" style="color:var(--muted);font-size:10.5px">
            {views.esc(sup["gstin"])}</div></td>
        <td class="r">{rupees(sup["exposure"])}</td>
        <td class="r" style="{"font-weight:600;color:var(--danger)"
                             if sup["at_risk"] else "color:var(--muted)"}">
          {rupees(sup["at_risk"])}</td>
        <td class="r">{ui.badge(f'{sup["trust_score"]}/100', tone)}</td>
        <td>{ui.badge(patterns.get(sup["pattern"], sup["pattern"]),
                      ui.TONE_BAD if sup["high_risk"] else ui.TONE_NEUTRAL,
                      title=sup["pattern"])}
          {'<div style="color:var(--muted);font-size:10.5px;margin-top:2px">'
           'not in the active source</div>'
           if not sup.get("history_known", True) else ''}</td>
        <td>{views.esc(actions.get(sup["action"], sup["action"]))}
          {f'<div style="color:var(--warn);font-size:11px;margin-top:2px">'
           f'the agent would go further</div>' if sup.get("goes_further") else ''}
        </td>
      </tr>"""


@app.post("/agents/input-credit")
async def start_supplier_risk(request: Request,
                              ws: Workspace = Depends(required_workspace)):
    """Read the upload, then hand it to a thread."""
    from urllib.parse import quote

    form = await request.form()
    upload = form.get("register")
    if upload is None or not getattr(upload, "filename", ""):
        return RedirectResponse(
            "/agents/input-credit?error=" + quote("Choose a file first."),
            status_code=303)

    data = await upload.read()
    if len(data) > 8 * 1024 * 1024:
        return RedirectResponse(
            "/agents/input-credit?error="
            + quote("That file is over 8 MB. A purchase register for one "
                    "period should be far smaller."),
            status_code=303)

    key = f"risk_{int(time.time() * 1000)}"
    with _risk_lock:
        RISK_RUNS[key] = {"state": "running", "phase": "Reading the file",
                          "done": 0, "total": 0}

    with ledger(ws.business_id) as led:
        AccessLog(led.conn).record(
            Action.RUN_AUDIT, user=ws.user, business_id=ws.business_id,
            target=key, detail="ran a supplier risk analysis")

    threading.Thread(
        target=_run_risk,
        args=(key, ws.business_id, data, upload.filename,
              form.get("use_agent") == "yes"),
        daemon=True).start()
    return RedirectResponse(f"/agents/input-credit?key={key}",
                            status_code=303)


@app.post("/agents/input-credit/demo")
async def run_demo_analysis(request: Request,
                            ws: Workspace = Depends(required_workspace)):
    """
    State 1, in one click: build both halves and analyse them.

    The register and the filing history are generated here rather than being
    offered as files to download and upload back. That round trip is theatre -
    the platform holds both halves already, and making a person fetch them only
    adds two clicks and a chance to pick the wrong file.

    What is NOT skipped is the pipeline. The generated register goes through
    the same parser a real CSV does, and the generated history goes out through
    the GSTN wire format and back through the same reader a live GSP response
    uses. A demo that took a shortcut past either would stop being evidence
    that the thing works.
    """
    form = await request.form()
    key = f"risk_{int(time.time() * 1000)}"
    with _risk_lock:
        RISK_RUNS[key] = {"state": "running", "phase": "Building demo data",
                          "done": 0, "total": 0}

    with ledger(ws.business_id) as led:
        AccessLog(led.conn).record(
            Action.RUN_AUDIT, user=ws.user, business_id=ws.business_id,
            target=key, detail="ran a supplier risk analysis on demo data")

    from merchant.purchase_import import SAMPLE_REGISTER

    threading.Thread(
        target=_run_risk,
        args=(key, ws.business_id, SAMPLE_REGISTER.encode(),
              "demo-register.csv", form.get("use_agent") == "yes"),
        daemon=True).start()
    return RedirectResponse(f"/agents/input-credit?key={key}",
                            status_code=303)


@app.post("/agents/input-credit/history")
async def upload_filing_history(request: Request,
                                ws: Workspace = Depends(required_workspace)):
    """
    Mode B: take a filing-history file and make it this business's source.

    Stored rather than held for one run. A merchant who went to the trouble of
    assembling three years of filing dates should not be asked for them again
    on the next page load, and the source badge has to be able to say what is
    live before any analysis has been started.
    """
    from urllib.parse import quote

    from merchant.purchase_import import parse_filing_history

    form = await request.form()
    upload = form.get("history")
    if upload is None or not getattr(upload, "filename", ""):
        return RedirectResponse(
            "/agents/input-credit?error=" + quote("Choose a file first."),
            status_code=303)

    data = await upload.read()
    if len(data) > 8 * 1024 * 1024:
        return RedirectResponse(
            "/agents/input-credit?error="
            + quote("That file is over 8 MB."), status_code=303)

    imported = parse_filing_history(data, upload.filename)
    if not imported.ok:
        why = (f"Missing columns: {', '.join(imported.missing_columns)}."
               if imported.missing_columns else
               "No supplier rows could be read - check the GSTIN and period "
               "columns.")
        return RedirectResponse(
            "/agents/input-credit?error=" + quote(why), status_code=303)

    with ledger(ws.business_id) as led:
        stored = led.replace_filing_history(imported)
        AccessLog(led.conn).record(
            Action.RUN_AUDIT, user=ws.user, business_id=ws.business_id,
            target=upload.filename,
            detail=f"uploaded filing history for {stored['suppliers']} suppliers")

    message = (f"Filing history for {stored['suppliers']} suppliers over "
               f"{stored['periods']} periods. Analyses now use this instead of "
               f"simulated records.")
    if imported.rows_skipped:
        message += f" {len(imported.rows_skipped)} rows were skipped."
    return RedirectResponse(
        "/agents/input-credit?ok=" + quote(message), status_code=303)


@app.post("/agents/input-credit/history/forget")
def forget_filing_history(ws: Workspace = Depends(required_workspace)):
    """Drop the uploaded history. The source falls back and says so."""
    from urllib.parse import quote

    with ledger(ws.business_id) as led:
        led.forget_filing_history()
    return RedirectResponse(
        "/agents/input-credit?ok="
        + quote("Filing history removed. Analyses fall back to simulated "
                "records until you upload again."), status_code=303)


@app.get("/agents/input-credit/sample-history")
def sample_history_file(ws: Workspace = Depends(required_workspace)):
    """
    The sample register's suppliers, in the upload format.

    Generated from the simulator on purpose: uploading it reproduces the
    simulated run exactly, which is how a person checks that the mode really
    does not change the answer rather than taking the claim on trust.
    """
    from fastapi.responses import PlainTextResponse

    from merchant.purchase_import import sample_filing_history

    return PlainTextResponse(
        sample_filing_history(), media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="sample-filing-history.csv"'})


@app.get("/agents/input-credit/sample")
def sample_register(ws: Workspace = Depends(required_workspace)):
    """A register somebody can try in one click."""
    from fastapi.responses import PlainTextResponse

    from merchant.purchase_import import SAMPLE_REGISTER

    return PlainTextResponse(
        SAMPLE_REGISTER, media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="sample-purchase-register.csv"'})


@app.get("/agents/input-credit/{key}.json")
def supplier_risk_json(key: str, ws: Workspace = Depends(required_workspace)):
    """
    The payload, for anything that wants it without the HTML.

    Portfolio totals, then one entry per supplier with their invoices nested
    underneath - so a row can always be traced back to what it was built from.
    """
    with _risk_lock:
        state = dict(RISK_RUNS.get(key) or {})
    if not state:
        return JSONResponse({"error": "no such analysis"}, status_code=404)
    if state.get("state") != "done":
        return JSONResponse({"state": state.get("state"),
                             "phase": state.get("phase", "")})
    return JSONResponse(state["payload"])


# --- purchases, and the input credit on them -------------------------------
#
# The other side of the books. Sales flow through /simulator and /settlements;
# purchases flow through here and /itc. They share the business they belong to
# and nothing else, which is why they are separate pages rather than tabs on
# one - a merchant reconciling what they were charged and a merchant chasing a
# supplier who did not file are doing two unrelated jobs on the same afternoon.

PURCHASE_CATEGORIES = {
    "": "Ordinary business purchase",
    "food_beverage": "Food, beverages or catering",
    "motor_vehicle": "Motor vehicle",
    "club_membership": "Club or gym membership",
    "works_contract_immovable": "Works contract on a building",
    "personal_consumption": "Personal consumption",
}


@app.post("/agents/input-credit/gstr2b")
async def import_gstr2b(request: Request,
                        ws: Workspace = Depends(required_workspace)):
    """
    Take one or more GSTR-2B downloads.

    Several at once on purpose: the supplier watch needs at least three periods
    to tell a supplier who has STOPPED filing from one who simply has not filed
    yet, so importing a month at a time is the slow road to a feature that
    cannot work.
    """
    from urllib.parse import quote

    from merchant.gstr2b_import import parse_many

    ws.require_owner("where this business gets its GSTR-2B", request)

    form = await request.form()
    uploads = form.getlist("gstr2b")
    files = []
    for upload in uploads:
        name = getattr(upload, "filename", "")
        if not name:
            continue
        data = await upload.read()
        if len(data) > 20 * 1024 * 1024:
            return RedirectResponse(
                "/agents/input-credit?error="
                + quote(f"{name} is over 20 MB."), status_code=303)
        files.append((data, name))

    if not files:
        return RedirectResponse(
            "/agents/input-credit?error="
            + quote("Choose at least one GSTR-2B file."), status_code=303)

    parsed, problems = parse_many(files)
    if not parsed:
        return RedirectResponse(
            "/agents/input-credit?error="
            + quote("; ".join(problems) or "Nothing could be read."),
            status_code=303)

    added = replaced = blocked = 0
    periods = []
    with ledger(ws.business_id) as led:
        for one in parsed:
            outcome = led.import_gstr2b(one.lines, one.period)
            added += outcome["added"]
            replaced += outcome["replaced"]
            blocked += len(one.blocked_by_gstn)
            periods.append(one.period or "an unnamed period")
        AccessLog(led.conn).record(
            Action.CONNECT_SOURCE, user=ws.user, business_id=ws.business_id,
            detail=f"imported GSTR-2B for {', '.join(periods)}")

    message = (f"Imported {added} lines across {len(parsed)} period"
               f"{'' if len(parsed) == 1 else 's'} ({', '.join(periods)})")
    if replaced:
        message += f", replacing {replaced} already held"
    message += "."
    if blocked:
        message += (f" GSTN marks {blocked} of them as credit NOT available - "
                    f"the reconciler will flag those.")
    if problems:
        message += " " + "; ".join(problems)

    return RedirectResponse(
        "/agents/input-credit?ok=" + quote(message), status_code=303)


@app.post("/itc/run")
def run_itc(request: Request, use_agent: str = Form("yes"),
            ws: Workspace = Depends(required_workspace)):
    """
    Reconcile the unreconciled purchases.

    Runs on the same thread pool, progress dict and terminal as the settlement
    auditor. Nothing here is agent-specific except which runner is started.
    """
    resolved = ws.business_id

    with ledger(resolved) as led:
        if not led.businesses.agent_enabled(resolved, "gst_itc"):
            return RedirectResponse(
                "/agents/input-credit/purchases?error=The+reconciler+is+switched+off+for+this+business.",
                status_code=303)
        if not led.unreconciled_purchases():
            return RedirectResponse(
                "/agents/input-credit/purchases?error=There+is+nothing+left+to+reconcile.",
                status_code=303)
        card = led.rate_card()

    spec = catalog.get("gst_itc")
    key = f"itc_{int(time.time() * 1000)}"
    with _lock:
        RUNS[key] = {"state": "running", "phase": "Starting", "done": 0,
                     "total": 0, "settled_by_rules": 0, "current": "",
                     "note": "", "lines": [], "results": [],
                     "agent": spec.name, "started": time.time()}

    with ledger(resolved) as led:
        AccessLog(led.conn).record(
            Action.RUN_AUDIT, user=ws.user, business_id=resolved,
            target=key, address="")

    ctx = AgentContext(business_id=resolved, rate_card=card, db=DB,
                       target_id=key, use_agent=(use_agent == "yes"),
                       progress=_progress(key))
    threading.Thread(target=_run_agent, args=("gst_itc", ctx),
                     daemon=True).start()
    return RedirectResponse(f"/itc/{key}", status_code=303)


MESSAGE_BOX = (
    '<div style="margin-top:12px;padding:11px 13px;'
    'background:var(--raised);border:1px solid var(--line-2);'
    'border-radius:7px;font-size:12.3px;white-space:pre-wrap">'
    "{body}</div>")


@app.post("/suppliers/status")
def record_gstin_status(request: Request, gstin: str = Form(...),
                        status: str = Form(...),
                        cancelled_on: str = Form(""),
                        ws: Workspace = Depends(required_workspace)):
    """
    What a person saw on the GST portal, typed in.

    The portal's public search is behind a captcha, so this is the honest path
    when nobody has a verification API key: a human looks, and says what they
    saw. Recorded as entered by hand, never dressed up as a lookup.
    """
    from urllib.parse import quote

    from merchant.gstin_lookup import GstinStatus, normalise_status
    from engine.gst.watch import STATUS_UNKNOWN

    ws.require_owner("what this business believes about its suppliers", request)

    cleaned = gstin.strip().upper()
    if len(cleaned) != 15:
        return RedirectResponse(
            "/agents/input-credit?error=" + quote(f"{cleaned} is not a 15-character GSTIN."),
            status_code=303)
    if normalise_status(status) == STATUS_UNKNOWN:
        return RedirectResponse(
            "/agents/input-credit?error=" + quote("Pick active, suspended or cancelled."),
            status_code=303)

    with ledger(ws.business_id) as led:
        GstinStatus(led.conn).record_manual(
            cleaned, status, cancelled_on.strip() or None)
        AccessLog(led.conn).record(
            Action.CONNECT_SOURCE, user=ws.user, business_id=ws.business_id,
            target=cleaned,
            detail=f"recorded {cleaned} as {normalise_status(status)} by hand")

    return RedirectResponse(
        "/agents/input-credit?ok=" + quote(f"Recorded. Run a check to apply it."),
        status_code=303)


@app.get("/agents/input-credit/reconciliation", response_class=HTMLResponse)
def suppliers_page(ws: Workspace = Depends(required_workspace),
                   error: str = "", ok: str = ""):
    """
    Who is holding your money, and what changed since last time.

    Two questions on one page on purpose. The register is the STATE; the raised
    cards are the NEWS. A merchant who only ever sees state has to work out
    what moved themselves, which is the job the watch exists to do for them.
    """
    from engine.gst.watch import (DEAD_STATUSES, MIN_INVOICES_FOR_RATE,
                                  MIN_PERIODS_TO_JUDGE, STATUS_UNKNOWN)

    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        _head = _workspace_head(led, ws, "gst_itc", "")
        _needs = _setup_banner(_requirements(led, ws, "gst_itc"))
        last = led.last_check()
        gstr2b_held = led.gstr2b_periods()
        register = led.supplier_register()
        raised = led.raised_in(last["check_id"]) if last else []
        looked_at = (led.raised_in(last["check_id"], only_raised=False)
                     if last else [])
        quiet = [r for r in looked_at if not r["raise_it"]]
        checks = led.watch_checks(10)
        periods = {r["period"] for r in checks if r["period"]}
        enabled = led.businesses.agent_enabled(ws.business_id, "gst_itc")
        has_purchases = bool(led.purchases(1))
        from merchant.gstin_lookup import GstinStatus

        lookups = GstinStatus(led.conn)
        known = {r["gstin"]: lookups.get(r["gstin"]) for r in register}

    URGENCY_PILL = {"now": "danger", "this_week": "warn", "this_month": "",
                    "no_action": ""}
    URGENCY_LABEL = {"now": "Do this now", "this_week": "This week",
                     "this_month": "This month", "no_action": "No action"}
    URGENCY_ACCENT = {"now": "var(--danger)", "this_week": "var(--warn)",
                      "this_month": "var(--line)", "no_action": "var(--line)"}
    ACTION_LABEL_ITC = {
        "chase_supplier": "Chase the supplier",
        "stop_buying": "Stop buying from them",
        "reverse_claim": "Reverse the claim",
        "tell_accountant": "Tell your accountant",
        "watch": "Keep watching", "nothing": "Nothing"}

    banner = ""
    if error:
        banner = '<div class="banner warn"><span>' + views.esc(error) + "</span></div>"
    elif ok:
        banner = '<div class="banner brand"><span>' + views.esc(ok) + "</span></div>"
    if not enabled:
        banner += ('<div class="banner warn">The input credit reconciler is '
                   'turned off for this business, so nothing is being '
                   'watched.</div>')

    def raised_card(r) -> str:
        urgency = r["urgency"]
        accent = URGENCY_ACCENT.get(urgency, "var(--line)")
        message = ""
        if r["supplier_message"]:
            # Folded away by default. With three or four raised items an
            # always-open draft dominates the page, and the merchant needs to
            # decide WHETHER to send before they need to read what it says.
            message = f"""
  <details style="margin-top:11px">
    <summary style="cursor:pointer;color:var(--brand-ink);font-size:12px">
      Read the message we drafted</summary>
    {MESSAGE_BOX.format(body=views.esc(r["supplier_message"]))}
  </details>"""
        return f"""
<div class="card" style="border-left:3px solid {accent}">
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:7px">
    <span class="pill {URGENCY_PILL.get(urgency, "")}">
      {views.esc(URGENCY_LABEL.get(urgency, urgency))}</span>
    <span class="pill">{views.esc(ACTION_LABEL_ITC.get(r["action"], r["action"]))}</span>
    <span style="color:var(--muted);font-size:11.5px;margin-left:auto">
      {views.esc(r["name"])}</span>
  </div>
  <div style="font-weight:580">{views.esc(r["headline"])}</div>
  <p class="sub" style="margin:5px 0 0">{views.esc(r["reasoning"])}</p>
  {message}
</div>"""

    def register_row(r) -> str:
        dead = r["status"] in DEAD_STATUSES
        # Below the threshold this says so instead of printing a percentage.
        # One unfiled invoice reading "0%" beside a supplier's "100%" from
        # twelve invited a conclusion the evidence could not support.
        enough = r["invoices_booked"] >= MIN_INVOICES_FOR_RATE
        rate = (f'{r["invoices_filed"] * 100 // r["invoices_booked"]}%'
                if enough else
                '<span style="color:var(--faint);font-size:11.5px">too new'
                '</span>')
        found = known.get(r["gstin"])
        if dead:
            badge = f'<span class="pill danger">{views.esc(r["status"])}</span>'
        elif found is not None and found.known and found.stale_after():
            # Deliberately not shown as its old status. A month-old "active"
            # is not evidence a registration is alive today.
            badge = ('<span style="color:var(--warn);font-size:11.5px">'
                     'checked long ago</span>')
        elif r["status"] == STATUS_UNKNOWN:
            # Muted text rather than a pill. Most rows are unchecked, and four
            # grey pills crowd out the one that says "cancelled".
            badge = ('<span style="color:var(--faint);font-size:11.5px">'
                     'not checked</span>')
        else:
            badge = f'<span class="pill good">{views.esc(r["status"])}</span>'
        weight = "font-weight:580" if r["exposed_paise"] else "color:var(--muted)"
        return f"""
      <tr>
        <td>{views.esc(r["name"])}
          <div class="mono" style="color:var(--muted);font-size:10.5px">
            {views.esc(r["gstin"])}</div></td>
        <td class="r">{r["invoices_filed"]}/{r["invoices_booked"]}</td>
        <td class="r">{rate}</td>
        <td class="r" style="{weight}">{rupees(r["exposed_paise"])}</td>
        <td class="r" style="color:var(--muted)">
          {views.esc(r["last_filed_period"] or "never")}</td>
        <td>{badge}</td>
      </tr>"""

    if not last:
        head = "Nothing checked yet"
        sub = "Run a check to see who is holding your credit."
    elif raised:
        head = f'{len(raised)} thing{"" if len(raised) == 1 else "s"} worth your attention'
        sub = (f'{len(quiet)} other change{"" if len(quiet) == 1 else "s"} '
               f'were looked at and left alone.' if quiet
               else "Everything else was unchanged.")
    else:
        head = "Nothing needs you"
        sub = ("Nothing changed since the last check." if not quiet else
               f'{len(quiet)} change{"" if len(quiet) == 1 else "s"} were '
               f'looked at and none were worth raising.')

    check_button = ('<button>Check now</button>' if has_purchases and enabled
                    else '<span class="sub">Record some purchases first.</span>')

    # How much history this judgment rests on. A stoppage cannot be seen at all
    # without enough periods to observe one - a supplier has to have filed,
    # then stopped - so a thin history is stated rather than papered over.
    spread = len(periods)
    if spread >= MIN_PERIODS_TO_JUDGE:
        history_note = (
            f"Judged on {spread} periods of your purchase history. That is "
            f"enough to tell a supplier who has stopped filing from one who "
            f"missed a month.")
    else:
        history_note = (
            f"Judged on {spread} period{'' if spread == 1 else 's'} of "
            f"history. Filing rates are shown, but this is not yet enough to "
            f"tell a supplier who has STOPPED filing from one who has simply "
            f"not filed yet &mdash; that needs at least "
            f"{MIN_PERIODS_TO_JUDGE} periods. Import a few months of GSTR-2B "
            f"and the watch gets sharper.")
    exposed_total = sum(r["exposed_paise"] for r in register)

    history = ""
    if len(checks) > 1:
        rows = "".join(f"""<tr>
      <td class="r" style="color:var(--muted)">{views.when(c["at"])}</td>
      <td class="r">{c["suppliers"]}</td>
      <td class="r">{rupees(c["exposed_paise"])}</td>
      <td class="r">{c["changes_found"]}</td>
      <td class="r">{c["raised"]}</td></tr>""" for c in checks)
        history = f"""
<div class="card flush">
  <div class="card-head"><h2>Every check</h2></div>
  <table>
    <tr><th class="r">When</th><th class="r">Suppliers</th>
        <th class="r">Exposed</th><th class="r">Changed</th>
        <th class="r">Raised</th></tr>
    {rows}
  </table>
</div>"""

    empty_register = ('<tr><td colspan="6" class="empty">'
                      '<div style="font-weight:560;color:var(--ink);'
                      'margin-bottom:4px">No suppliers yet</div>'
                      'Record purchases, then run a check.</td></tr>')

    body = f"""
{banner}
{_head}
{_needs}
<p class="sub">Your tax credit depends on other people filing their returns.
   This is who is holding it, and what changed since the last check.</p>

<div class="card">
  <div class="row" style="align-items:center">
    <div>
      <h2 style="margin:0">{views.esc(head)}</h2>
      <p class="sub" style="margin:3px 0 0">{views.esc(sub)}</p>
    </div>
    <div style="flex:0">
      <form method="post" action="/suppliers/check">{check_button}</form>
    </div>
  </div>
</div>

{"".join(raised_card(r) for r in raised)}

{views.gstr2b_import_card(gstr2b_held)}

<div class="card flush">
  <div class="card-head">
    <h2>Who is holding your credit</h2>
    <span class="sub">{rupees(exposed_total)} unsupported</span>
  </div>
  <table>
    <tr><th>Supplier</th><th class="r">Filed</th><th class="r">Rate</th>
        <th class="r">Exposed</th><th class="r">Last filed</th>
        <th>Registration</th></tr>
    {"".join(register_row(r) for r in register) or empty_register}
  </table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    The rate is what they have actually filed, not a prediction of what they
    will file. &ldquo;Filed 3 of 6&rdquo; is a fact; a percentage likelihood
    would be a guess nobody can check.
  </div>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    {history_note}
  </div>
</div>

<details class="card">
  <summary style="cursor:pointer;font-weight:580;font-size:13.5px">
    Record a registration status</summary>
  <p class="sub" style="margin:9px 0 12px">The GST portal&rsquo;s public search
     is behind a captcha, so this platform does not read it for you. Look a
     supplier up at
     <b>services.gst.gov.in/services/searchtp</b> and record what you saw. It
     is marked as entered by hand, because that is what it is.</p>
  <form method="post" action="/suppliers/status">
    <div class="row">
      <div><label>GSTIN</label>
        <input name="gstin" placeholder="27AABCU9603R1ZM" required></div>
      <div><label>Status</label>
        <select name="status">
          <option value="active">Active</option>
          <option value="suspended">Suspended</option>
          <option value="cancelled">Cancelled</option>
        </select></div>
      <div><label>Cancelled on (if it is)</label>
        <input name="cancelled_on" placeholder="14/05/2026"></div>
      <div style="flex:0;align-self:flex-end"><button>Record</button></div>
    </div>
  </form>
</details>

{history}"""
    return views.page("Suppliers", body, "agent:gst_itc", **shell)


@app.post("/suppliers/check")
def check_suppliers(request: Request, use_agent: str = Form("yes"),
                    ws: Workspace = Depends(required_workspace)):
    from merchant.agents.gst import run_supplier_watch

    resolved = ws.business_id
    with ledger(resolved) as led:
        if not led.businesses.agent_enabled(resolved, "gst_itc"):
            return RedirectResponse(
                "/agents/input-credit?error=The+reconciler+is+switched+off+for+this+business.",
                status_code=303)
        if not led.purchases(1):
            return RedirectResponse(
                "/agents/input-credit?error=There+are+no+purchases+to+watch+yet.",
                status_code=303)
        card = led.rate_card()

    key = f"chk_{int(time.time() * 1000)}"
    with _lock:
        RUNS[key] = {"state": "running", "phase": "Starting", "done": 0,
                     "total": 0, "settled_by_rules": 0, "current": "",
                     "note": "", "lines": [], "results": [],
                     "agent": "Supplier watch", "started": time.time()}

    with ledger(resolved) as led:
        AccessLog(led.conn).record(
            Action.RUN_AUDIT, user=ws.user, business_id=resolved,
            target=key, address="", detail="ran the supplier watch")

    ctx = AgentContext(business_id=resolved, rate_card=card, db=DB,
                       target_id=key, use_agent=(use_agent == "yes"),
                       progress=_progress(key))
    threading.Thread(target=_run_agent, args=("gst_itc", ctx),
                     kwargs={"runner": run_supplier_watch},
                     daemon=True).start()
    return RedirectResponse(f"/itc/{key}", status_code=303)


# --- what a reconciliation concluded ---------------------------------------
#
# This page used to render the run's narration: every line the agent emitted,
# including "looking up invoice_detail" and "Asking the agent", in one column.
# That is a log, and a log is the right thing for whoever built the system and
# the wrong thing for whoever owns the money.
#
# It now renders the stored findings instead. The narration still exists - it
# moved behind a per-finding disclosure, because a merchant deciding whether to
# chase a supplier needs the decision first and the derivation only if they
# doubt it.

# What went wrong, in the merchant's words rather than ours.
ISSUE_LABEL = {
    "SUPPLIER_NOT_FILED": "Missing filing",
    "SUPPLIER_LATE_FILED": "Filed late",
    "GSTIN_MISMATCH": "Wrong GSTIN",
    "AMOUNT_MISMATCH": "Tax mismatch",
    "BLOCKED_CREDIT": "Not claimable",
    "TIME_BARRED": "Past deadline",
    "RULE_37_REVERSAL": "Supplier unpaid",
    "DUPLICATE_CLAIM": "Claimed twice",
    "NOT_IN_BOOKS": "Missing invoice",
    "UNEXPLAINED": "Needs a person",
}

ISSUE_EXPLAIN = {
    "SUPPLIER_NOT_FILED": "Your supplier has not reported this invoice to the "
                          "government, so the credit does not exist yet.",
    "SUPPLIER_LATE_FILED": "Reported a month later than your books expect. The "
                           "credit is intact, it just lands next period.",
    "GSTIN_MISMATCH": "Filed against a different registration. The credit "
                      "exists but is sitting in the wrong place.",
    "AMOUNT_MISMATCH": "Your supplier reported less tax than you were charged.",
    "BLOCKED_CREDIT": "This category is never claimable, however correctly it "
                      "was filed.",
    "TIME_BARRED": "The deadline to claim this has passed.",
    "RULE_37_REVERSAL": "You have not paid this supplier within 180 days, so "
                        "credit already taken has to be given back.",
    "DUPLICATE_CLAIM": "The same invoice is booked twice. GSTR-2B supports it "
                       "once.",
    "NOT_IN_BOOKS": "The government has this invoice and your books do not.",
    "UNEXPLAINED": "The evidence fitted nothing cleanly, so it was left for a "
                   "person rather than guessed at.",
}

RECOMMENDATION = {
    "chase_supplier": "Chase the supplier to amend their GSTR-2B filing.",
    "do_not_claim": "Do not claim this credit.",
    "reverse": "Reverse this credit before your next return.",
    "fix_books": "Correct your books to match.",
    "escalate": "Have someone look at this before you file.",
    "none": "Nothing to do.",
}


def _finding_card(row) -> str:
    """One invoice that needs attention, with the working folded away."""
    import json

    code = row["exception_code"]
    short = rules_gst.rupees(abs(row["money_at_stake"] or 0))
    # Named for what it actually is on this kind of finding. "At stake" is
    # right for a whole claim in doubt and wrong for a partial shortfall.
    stake_label = {
        "AMOUNT_MISMATCH": "Short by",
        "BLOCKED_CREDIT": "Must not claim",
        "TIME_BARRED": "Lost",
        "RULE_37_REVERSAL": "Must reverse",
        "DUPLICATE_CLAIM": "Claimed twice",
    }.get(code, "At risk")
    deadline = row["claim_deadline"]
    days = row["days_to_deadline"]

    urgent = days is not None and days < 90
    when = ""
    if deadline:
        when = (f'<div class="fact"><span>Deadline to fix</span>'
                f'<b style="{"color:var(--danger)" if urgent else ""}">'
                f'{views.esc(_pretty_date(deadline))}</b>'
                f'<em>{days} days left</em></div>' if days is not None else "")

    evidence = json.loads(row["evidence"] or "[]")
    working = "".join(
        f'<li style="margin-bottom:7px">{views.esc(e["detail"])}'
        f'<div style="color:var(--muted);font-size:11.4px;margin-top:2px">'
        f'{views.esc(e["rule"])} &mdash; {views.esc(e["source"])}</div></li>'
        for e in evidence)

    split = (f'CGST {rules_gst.rupees(row["cgst"])} + '
             f'SGST {rules_gst.rupees(row["sgst"])}'
             if not row["igst"] else f'IGST {rules_gst.rupees(row["igst"])}')

    message = ""
    if row["supplier_message"]:
        message = (
            '<details class="working" style="border-top:0;padding-top:9px">'
            '<summary>Read the message we drafted</summary>'
            f'<div class="draft">{views.esc(row["supplier_message"])}</div>'
            '</details>')

    return f"""
<div class="finding-card">
  <div class="finding-card-top">
    <div>
      <div class="finding-card-who">{views.esc(row["supplier_name"])}</div>
      <div class="finding-card-inv">Invoice {views.esc(row["invoice_number"])}
        &middot; {views.esc(_pretty_date(row["invoice_date"]))}</div>
    </div>
    {ui.badge(ISSUE_LABEL.get(code, code.replace("_", " ").capitalize()),
              ui.CODE_TONE.get(code, ""), title=code)}
  </div>

  <p class="finding-card-why">{views.esc(ISSUE_EXPLAIN.get(code, ""))}</p>

  <div class="facts">
    <div class="fact"><span>{views.esc(stake_label)}</span>
      <b style="color:var(--danger)">{short}</b></div>
    <div class="fact"><span>You claimed</span>
      <b>{rules_gst.rupees(row["claimed_tax"])}</b></div>
    <div class="fact"><span>GSTR-2B supports</span>
      <b>{rules_gst.rupees(row["available_tax"])}</b></div>
    {when}
  </div>

  <div class="recommend">
    <span class="recommend-label">Recommended</span>
    {views.esc(RECOMMENDATION.get(row["action"], row["action"]))}
  </div>
  {message}

  <details class="working">
    <summary>Show the working</summary>
    <div class="working-body">
      <div class="working-line"><span>Taxable value on the invoice</span>
        <b>{rules_gst.rupees(row["taxable_value"])}</b></div>
      <div class="working-line"><span>Tax split</span><b>{split}</b></div>
      <div class="working-line"><span>Claimed in your books</span>
        <b>{rules_gst.rupees(row["claimed_tax"])}</b></div>
      <div class="working-line"><span>Reported by the supplier</span>
        <b>{rules_gst.rupees(row["available_tax"])}</b></div>
      <div class="working-line"><span>Difference</span>
        <b>{rules_gst.rupees(abs(row["delta"]))}</b></div>
      <div class="working-line"><span>Tolerance before it counts</span>
        <b>{rules_gst.rupees(row["tolerance"])}</b></div>
      {f'<ul style="margin:12px 0 0;padding-left:18px;font-size:12.6px">{working}</ul>' if working else ''}
      <p style="margin:12px 0 0;font-size:11.5px;color:var(--muted)">
        Decided by {views.esc("the rate card" if row["decided_by"] == "calculator" else "the agent")}
        {f'at {row["confidence"]:.0%} confidence' if row["decided_by"] != "calculator" else "- arithmetic, not judgment"}.
        {views.esc(row["rule_cited"] or "")}
      </p>
    </div>
  </details>
</div>"""


def _pretty_date(iso: str) -> str:
    """2027-11-30 as 30 Nov 2027. Nobody reads a tax deadline in ISO."""
    from datetime import date

    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d %b %Y")
    except (ValueError, TypeError):
        return str(iso or "")


@app.get("/itc/{key}", response_class=HTMLResponse)
def itc_run_page(key: str, ws: Workspace = Depends(required_workspace)):
    """While it runs, progress. Once it is done, what it concluded."""
    with _lock:
        state = dict(RUNS.get(key) or {})

    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        head = _workspace_head(led, ws, "gst_itc", "")

        run_id = state.get("run_id")
        if not state and not run_id:
            return RedirectResponse("/agents/input-credit", status_code=303)
        if run_id is None and state.get("state") != "running":
            latest = led.latest_itc_run()
            run_id = latest["run_id"] if latest else None

        findings = led.itc_findings(run_id) if run_id else []

    running = state.get("state") == "running"
    if running or not findings:
        return HTMLResponse(_reconciling(state, head, shell))

    action = [f for f in findings
              if f["exception_code"] not in {"CLAIM_CLEAN", "ROUNDING"}]
    clean = len(findings) - len(action)

    claimed = sum(f["claimed_tax"] for f in findings)
    at_risk = sum(abs(f["money_at_stake"] or 0) for f in action)
    # Derived, so the three figures always add up. Computing "supported"
    # independently let it drift from the other two, and a summary whose own
    # numbers disagree is worse than no summary.
    safe = claimed - at_risk

    cards = "".join(_finding_card(f) for f in action)

    if action:
        section = (
            '<div style="margin:22px 0 11px;display:flex;align-items:baseline;'
            'gap:9px"><h2 style="margin:0">Needs review</h2>'
            f'<span class="sub">{len(action)} invoice'
            f'{"" if len(action) == 1 else "s"}</span></div>{cards}')
    else:
        section = ui.blank_slate(
            "Everything reconciled",
            f"All {len(findings)} invoices match what your suppliers reported. "
            f"Nothing needs chasing.")

    body = f"""
{head}

<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here has been filed, amended or claimed.</b>
  Every line is a proposal waiting for you.</span>
</div>

<div class="card" style="padding:0;overflow:hidden;margin-bottom:16px">
  <div class="stats">
    <div class="stat"><b>{rules_gst.rupees(claimed)}</b>
      <span>claimed in your books</span></div>
    <div class="stat"><b style="color:var(--good)">{rules_gst.rupees(safe)}</b>
      <span>safe to claim</span></div>
    <div class="stat"><b style="color:var(--danger)">{rules_gst.rupees(at_risk)}</b>
      <span>needs your attention</span></div>
    <div class="stat"><b>{clean}/{len(findings)}</b>
      <span>invoices clean</span></div>
  </div>
</div>

{section}

<div class="card tint" style="margin-top:20px">
  <h2>How this was worked out</h2>
  <p class="sub" style="margin:4px 0 0">Every figure above came from your
     purchase register and the GSTR-2B lines your suppliers filed &mdash;
     compared by arithmetic, never estimated. Where the evidence pointed more
     than one way, the agent chose and said why; open <em>Show the working</em>
     on any finding to see exactly what it compared.</p>
</div>"""
    return views.page("Reconciliation", body, "agent:gst_itc", **shell)


def _reconciling(state: dict, head: str, shell: dict) -> str:
    """
    While it runs.

    Deliberately not the trace. The tool calls and the agent's deliberation are
    interesting to whoever built this and noise to whoever owns the money, so
    what shows is progress and the findings as they land.
    """
    phase = state.get("phase") or "Starting"
    found = [l for l in state.get("lines", [])
             if l.get("kind") in ("finding", "total")]
    rows = "".join(
        f'<div class="found-line">{views.esc(l.get("text", ""))}</div>'
        for l in found)

    failed = state.get("state") == "failed"
    body = f"""
{head}
<div class="card">
  <div style="display:flex;align-items:center;gap:11px">
    {'' if failed else '<span class="spinner"></span>'}
    <div>
      <div style="font-weight:580">
        {views.esc("Could not finish" if failed else "Checking your invoices against GSTR-2B")}</div>
      <div class="sub" style="margin-top:2px">{views.esc(phase)}</div>
    </div>
  </div>
  {f'<div class="found">{rows}</div>' if rows else ''}
</div>
{'' if failed else '<meta http-equiv="refresh" content="1">'}"""
    return views.page("Reconciling", body, "agent:gst_itc", **shell)


# --- what each agent is doing, gathered once -------------------------------
#
# Both the hub and the home dashboard need the same picture of every agent, and
# they got it two different ways before this existed - which is how a card said
# "active" while the page it linked to said "setup needed".


def _agent_state(led, ws, spec, source_kind) -> str:
    """One agent's operational state, in the vocabulary merchant/ui.py uses."""
    from merchant.sources import SourceKind

    if not spec.is_live:
        return ui.STATE_SOON
    if not led.businesses.agent_enabled(ws.business_id, spec.id):
        return ui.STATE_OFF
    if spec.id == "settlement_audit":
        if source_kind is None:
            return ui.STATE_SETUP
        return (ui.STATE_DEMO if source_kind == SourceKind.SIMULATOR
                else ui.STATE_ACTIVE)
    if spec.id == "gst_itc":
        # The DATA SOURCE decides first, exactly as it does for the risk
        # screen's three modes. It used to be decided by whether any purchase
        # row was marked "imported", which was true while the only way to get
        # one was a merchant uploading a file - and stopped being true the
        # moment the demo button started generating a register and storing it
        # the same way. A business on the simulator was then badged "Live
        # data" on the strength of data the platform had invented.
        if source_kind is None or source_kind == SourceKind.SIMULATOR:
            return ui.STATE_DEMO
        if not led.purchases(1):
            return ui.STATE_SETUP
        return ui.STATE_ACTIVE
    return ui.STATE_SETUP


def _agent_metrics(led, ws, spec) -> list:
    """Two or three figures per agent. The ones a person would ask for first."""
    if spec.id == "settlement_audit":
        runs = led.settlements()
        recoverable = led.conn.execute(
            "SELECT COALESCE(SUM(money_at_stake),0) s FROM variances v"
            " JOIN business_runs br ON br.run_id = v.run_id"
            " WHERE br.business_id = ? AND v.exception_code IN"
            " ('ZERO_MDR_VIOLATION','INSTRUMENT_MISLABEL','RATE_MISMATCH',"
            "'MISSING_FROM_SETTLEMENT')", (ws.business_id,)).fetchone()["s"]
        return [(str(len(runs)), "settlements"),
                (rupees(recoverable or 0), "recoverable")]
    if spec.id == "gst_itc":
        last = led.last_check()
        exposed = last["exposed_paise"] if last else 0
        return [(str(len(led.purchases(limit=5_000))), "invoices"),
                (rupees(exposed), "credit at risk")]
    return []


def _agent_picture(led, ws) -> list:
    """Every agent, live or planned, with its state and figures."""
    from merchant.nav import route_for
    from merchant.sources import Sources

    kind = Sources(led.conn).kind(ws.business_id)
    out = []
    for spec in catalog.all_agents():
        route = route_for(spec.id)
        state = _agent_state(led, ws, spec, kind)
        out.append((spec, route, state,
                    _agent_metrics(led, ws, spec) if spec.is_live else []))
    return out


@app.get("/agents", response_class=HTMLResponse)
def agents_hub(ws: Workspace = Depends(required_workspace)):
    """
    Every agent on one screen, with the way into each.

    This replaces a settings page that listed agents with on/off switches. The
    switch is still here, but it is not the point - the point is that a person
    who wants to do some work can see what is available and get to it.
    """
    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        picture = _agent_picture(led, ws)

    cards = []
    for spec, route, state, metrics in picture:
        control = ""
        if spec.is_live:
            on = state != ui.STATE_OFF
            control = (
                f'<form method="post" action="/agents/{spec.id}/toggle">'
                f'<button class="ghost small">{"Turn off" if on else "Turn on"}'
                f'</button></form>')
        cards.append(ui.agent_card(ui.AgentCardData(
            name=spec.name, tagline=spec.tagline, state=state,
            href=route.href if route else "#", metrics=metrics,
            cta="Open workspace" if state != ui.STATE_SETUP else "Set it up",
            why_unbuilt=spec.why_unbuilt, control=control)))

    live = [p for p in picture if p[0].is_live]
    planned = [p for p in picture if not p[0].is_live]
    live_cards = cards[:len(live)] if False else [
        c for c, p in zip(cards, picture) if p[0].is_live]
    planned_cards = [c for c, p in zip(cards, picture) if not p[0].is_live]

    body = f"""
<h1>Agents</h1>
<p class="sub">Each one audits a different thing somebody else calculated for
   you. {len(live)} running, {len(planned)} on the way.</p>

<div style="margin:20px 0 9px;font-size:11.5px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted);font-weight:600">Running</div>
{ui.grid(live_cards)}

<div style="margin:26px 0 9px;font-size:11.5px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted);font-weight:600">On the way</div>
{ui.grid(planned_cards)}

<div class="card tint" style="margin-top:22px">
  <h2>Why these and not others</h2>
  <p class="sub" style="margin:4px 0 0">Every agent here audits a number
     somebody else worked out and had no reason to check. A planned one has no
     implementation and cannot be switched on for anyone &mdash; a convincing
     mock of a working reconciler is not a roadmap.</p>
</div>"""
    return views.page("Agents", body, "agents", **shell)


def _workspace_head(led, ws, agent_id: str, current_slug: str = "",
                    action: str = "") -> str:
    """
    The top of an agent workspace: who it is, how it is doing, where to go.

    Rendered from the same _agent_state the hub and the home dashboard use, so
    a card that says "demo data" cannot link to a page that claims otherwise.
    """
    from merchant.nav import route_for
    from merchant.sources import Sources

    spec = catalog.get(agent_id)
    route = route_for(agent_id)
    if spec is None or route is None:
        return ""

    kind = Sources(led.conn).kind(ws.business_id)
    state = _agent_state(led, ws, spec, kind)
    tab_row = ui.tabs([(t.label, route.tab_href(t), t.slug == current_slug)
                       for t in route.tabs])
    return ui.agent_header(spec.name, spec.tagline, state, tab_row, action)


# What each agent needs before it can do anything, and whether it has it.
def _requirements(led, ws, agent_id: str) -> list[tuple[bool, str, str, str]]:
    """(satisfied, what, detail, fix_href) - shown as the setup banner."""
    from merchant.sources import KIND_LABEL, SourceKind, Sources

    out = []
    enabled = led.businesses.agent_enabled(ws.business_id, agent_id)
    out.append((enabled, "Agent switched on",
                "Running for this business" if enabled
                else "Turned off, so nothing will be audited", "/agents"))

    if agent_id == "settlement_audit":
        kind = Sources(led.conn).kind(ws.business_id)
        out.append((kind is not None, "Data source connected",
                    KIND_LABEL[kind] if kind else "Nothing connected yet",
                    "/data"))
        card = led.rate_card()
        n = len(card["instruments"])
        out.append((n > 0, "Rate card configured",
                    f"{n} instruments, GST at "
                    f"{card['gst_rate_bps'] / 100:.0f}%", "/settings"))
    elif agent_id == "gst_itc":
        purchases = led.purchases(limit=5_000)
        out.append((bool(purchases), "Purchase register",
                    f"{len(purchases)} invoices recorded" if purchases
                    else "No purchases yet",
                    "/agents/input-credit"))
        filed = led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b WHERE business_id = ?",
            (ws.business_id,)).fetchone()["n"]
        out.append((bool(filed), "GSTR-2B lines",
                    f"{filed} lines from your suppliers" if filed
                    else "Nothing to reconcile against yet", "/data/zoho"))
    return out


def _setup_banner(requirements) -> str:
    """
    Shown only when something is missing.

    A checklist of green ticks on every visit is furniture. The banner earns
    its place by appearing when there is a reason to.
    """
    missing = [r for r in requirements if not r[0]]
    if not missing:
        return ""
    rows = "".join(
        f'<li style="margin-bottom:6px">{views.esc(what)} &mdash; '
        f'<span style="color:var(--muted)">{views.esc(detail)}</span> '
        f'<a href="{views.esc(href)}">fix</a></li>'
        for _ok, what, detail, href in missing)
    return f"""
<div class="card" style="border-left:3px solid var(--brand);margin-bottom:15px">
  <h2 style="margin:0 0 7px">Before this can run</h2>
  <ul style="margin:0;padding-left:19px;font-size:13px">{rows}</ul>
</div>"""


@app.get("/agents/settlement/setup", response_class=HTMLResponse)
def settlement_setup(ws: Workspace = Depends(required_workspace)):
    return _setup_page(ws, "settlement_audit")


@app.get("/agents/input-credit/setup", response_class=HTMLResponse)
def itc_setup(ws: Workspace = Depends(required_workspace), error: str = "",
              ok: str = ""):
    return _setup_page(ws, "gst_itc", error=error, ok=ok,
                       extra=_filing_api_card(ws))


def _filing_api_card(ws: Workspace) -> str:
    """
    Mode A configuration: a GSP or verification API the merchant has a key for.

    Generic on purpose, exactly like the registration-lookup provider. Half a
    dozen vendors sell this and they differ only in the URL, where the key
    goes, and how deeply they nested the government's own field names, so
    hard-coding one would be picking a favourite on a merchant's behalf and
    stranding anyone who already pays somebody else.
    """
    from merchant.sources import Sources
    from merchant.vault import Vault

    with ledger(ws.business_id) as led:
        config = Sources(led.conn).filing_api_config(ws.business_id)

    if config:
        warn = ""
        if not config["key_available"]:
            warn = ('<p class="sub" style="margin:9px 0 0;color:var(--warn)">'
                    'The stored key cannot be decrypted, so runs fall back to '
                    'whatever else is available rather than calling the API '
                    'without one. Re-enter it below.</p>')
        return f"""
<div class="card">
  <div class="card-head"><h2>GST filing-status API</h2>
    {ui.badge("connected", ui.TONE_GOOD)}</div>
  <p class="sub" style="margin:4px 0 0">
    <span class="mono">{views.esc(config["url_template"])}</span></p>
  <p class="sub" style="margin:7px 0 0">{views.esc(config["message"])}</p>
  {warn}
  <form method="post" action="/agents/input-credit/setup/filing-api/forget"
        style="margin-top:13px">
    <button class="ghost small">Disconnect</button>
  </form>
</div>"""

    vault_note = (
        "" if Vault.from_env() is not None else
        '<p class="sub" style="margin:9px 0 0;font-size:11.5px;'
        'color:var(--warn)">No encryption key is configured, so the API key '
        'will not be stored. Set <span class="mono">LEDGERLINE_SECRET_KEY'
        '</span> to keep it between runs.</p>')

    return f"""
<div class="card">
  <h2>GST filing-status API</h2>
  <p class="sub" style="margin:3px 0 12px;max-width:70ch">Optional. With a GSP
     or verification API key, each supplier&rsquo;s GSTR-1 and GSTR-3B filing
     dates are read live instead of being uploaded or simulated. Nothing else
     changes &mdash; the same arithmetic runs over the same contract.</p>
  <form method="post" action="/agents/input-credit/setup/filing-api">
    <div><label>Endpoint URL</label>
      <input name="url_template" required
        placeholder="https://your-provider.example/returns/{{gstin}}"></div>
    <p class="sub" style="margin:5px 0 11px;font-size:11.4px">Must be https and
      must contain <span class="mono">{{gstin}}</span> &mdash; that is where
      each supplier&rsquo;s number is substituted in.</p>
    <div class="row">
      <div><label>API key</label>
        <input name="api_key" type="password" autocomplete="off"></div>
      <div><label>Header name</label>
        <input name="key_header" placeholder="x-api-key"></div>
      <div><label>or query parameter</label>
        <input name="key_param" placeholder="api_key"></div>
    </div>
    <div class="row" style="margin-top:11px">
      <div><label>Test with a GSTIN (optional)</label>
        <input name="probe_gstin" placeholder="27AAAAA0000A1Z5"></div>
      <div style="flex:0;align-self:flex-end"><button>Save</button></div>
    </div>
  </form>
  {vault_note}
</div>"""


@app.post("/agents/input-credit/setup/filing-api")
async def connect_filing_api(request: Request,
                             ws: Workspace = Depends(required_workspace)):
    """
    Store a filing-status API. Owner only.

    Same reasoning as the rate card: whoever configures where filing history
    comes from decides what every supplier score on this platform is computed
    against, and that is not a thing staff should be able to change quietly.
    """
    from urllib.parse import quote

    from merchant.sources import Sources

    ws.require_owner("where supplier filing history comes from", request)

    form = await request.form()
    with ledger(ws.business_id) as led:
        result = Sources(led.conn).configure_filing_api(
            ws.business_id,
            url_template=str(form.get("url_template") or ""),
            api_key=str(form.get("api_key") or ""),
            key_header=str(form.get("key_header") or ""),
            key_param=str(form.get("key_param") or ""),
            probe_gstin=str(form.get("probe_gstin") or "").strip().upper())
        if result.ok:
            AccessLog(led.conn).record(
                Action.CONNECT_SOURCE, user=ws.user, business_id=ws.business_id,
                target="filing_api", detail="configured a filing-status API")

    field = "ok" if result.ok else "error"
    return RedirectResponse(
        f"/agents/input-credit/setup?{field}=" + quote(result.message),
        status_code=303)


@app.post("/agents/input-credit/setup/filing-api/forget")
def forget_filing_api(ws: Workspace = Depends(required_workspace)):
    from urllib.parse import quote

    from merchant.sources import Sources

    ws.require_owner("where supplier filing history comes from")

    with ledger(ws.business_id) as led:
        Sources(led.conn).disconnect_filing_api(ws.business_id)
    return RedirectResponse(
        "/agents/input-credit/setup?ok="
        + quote("Disconnected. Analyses fall back to an uploaded history, or "
                "to simulated records if there is none."), status_code=303)


def _setup_page(ws: Workspace, agent_id: str, *, error: str = "",
                ok: str = "", extra: str = ""):
    """What this agent needs, whether it has it, and where to go and fix it."""
    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        head = _workspace_head(led, ws, agent_id, "setup")
        requirements = _requirements(led, ws, agent_id)

    spec = catalog.get(agent_id)
    banner = ""
    if error:
        banner = f'<div class="banner warn"><span>{views.esc(error)}</span></div>'
    elif ok:
        banner = f'<div class="banner brand"><span>{views.esc(ok)}</span></div>'

    rows = "".join(f"""
      <tr>
        <td>{ui.badge("ready", ui.TONE_GOOD) if ok
             else ui.badge("needed", ui.TONE_BRAND)}</td>
        <td>{views.esc(what)}</td>
        <td style="color:var(--muted)">{views.esc(detail)}</td>
        <td class="r"><a class="btn ghost small"
          href="{views.esc(href)}">Open</a></td>
      </tr>""" for ok, what, detail, href in requirements)

    body = f"""
{head}
{banner}
<div class="card flush">
  <div class="card-head"><h2>What this agent needs</h2></div>
  <table>
    <tr><th></th><th>Requirement</th><th>State</th><th class="r"></th></tr>
    {rows}
  </table>
</div>
{extra}

<div class="card tint">
  <h2>What it argues from</h2>
  <p class="sub" style="margin:4px 0 0">{views.esc(spec.authority)}</p>
</div>

<div class="card tint">
  <h2>Why nobody else builds this</h2>
  <p class="sub" style="margin:4px 0 0">{views.esc(spec.why_unbuilt)}</p>
</div>"""
    return views.page("Setup", body, f"agent:{agent_id}", **shell)


# --- where the old URLs went ----------------------------------------------
#
# Bookmarks, muscle memory and the test suite all point at the old paths. A
# refactor that breaks all three at once is a refactor nobody can verify, so
# every moved page answers where it used to live and says where it went.


def _moved(new_path: str):
    """
    A path alias, not a page.

    Deliberately outside the auth dependency: it resolves to a location and
    nothing else, and the page it points at does its own checking. Adding a
    login guard here would mean a signed-out visitor gets sent to /login from
    the alias and then has to find the new URL again after signing in.
    """
    def go(request: Request):
        target = new_path
        for name, value in request.path_params.items():
            target = target.replace("{" + name + "}", str(value))
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=307)
    return go


for _old, _new in nav.MOVED.items():
    app.get(_old)(_moved(_new))


# --- settings -------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(ws: Workspace = Depends(required_workspace),
                  error: str = "", ok: str = ""):
    resolved = ws.business_id

    from merchant.businesses import REGULATED
    from merchant.sources import SourceKind, Sources

    with ledger(resolved) as led:
        shell = _shell_for(led, ws)
        card = led.rate_card()
        behaviour = led.behaviour()
        source = Sources(led.conn).kind(resolved)
        deletable = led.businesses.may_delete(resolved)
        settled = led.businesses.settlement_count(resolved)
        sales = led.conn.execute(
            "SELECT COUNT(*) n FROM live_payments WHERE business_id = ?",
            (resolved,)).fetchone()["n"]
        biz_name = shell["business"]["name"]

    # --- the rate card -----------------------------------------------------
    #
    # A worked example per row, because "0.40%" is abstract and "Rs 4.72 on a
    # Rs 1,000 sale" is not. The auditor argues in rupees; the settings page
    # should too.
    SAMPLE = 100_000
    rows = []
    for key, spec in sorted(card["instruments"].items(),
                            key=lambda kv: kv[1]["label"]):
        capped = spec.get("network_mdr_cap_bps") is not None
        total_bps = spec["network_mdr_bps"] + spec["platform_fee_bps"]
        fee = (SAMPLE * total_bps + 5_000) // 10_000
        gst = (fee * card["gst_rate_bps"] + 5_000) // 10_000
        rows.append(f"""
      <tr>
        <td>
          <div>{views.esc(spec["label"])}</div>
          {f'<div style="color:var(--muted);font-size:10.5px;margin-top:1px">'
           f'&#128274; {views.esc(REGULATED.get(key, "regulated"))}</div>'
           if capped else ''}
        </td>
        <td class="r">
          <form method="post" action="/settings/rate" style="display:flex;
            gap:5px;justify-content:flex-end;align-items:center">
            <input type="hidden" name="instrument" value="{views.esc(key)}">
            <input name="network_pct" type="number" step="0.01" min="0"
              value="{spec["network_mdr_bps"] / 100:.2f}"
              style="width:68px;text-align:right"
              {'readonly title="Set by regulation. A contract cannot agree to more."'
               if capped else ''}>
            <input name="platform_pct" type="number" step="0.01" min="0"
              value="{spec["platform_fee_bps"] / 100:.2f}"
              style="width:68px;text-align:right">
            <button class="ghost small">save</button>
          </form>
        </td>
        <td class="r"><b>{total_bps / 100:.2f}%</b></td>
        <td class="r" style="color:var(--muted)">
          {rupees(fee)} + {rupees(gst)} GST</td>
      </tr>""")

    banner = ""
    if error:
        banner = (f'<div class="banner warn"><b>Not saved</b>'
                  f'<span>{views.esc(error)}</span></div>')
    elif ok:
        banner = (f'<div class="banner brand"><b>Saved</b>'
                  f'<span>{views.esc(ok)}</span></div>')

    # --- removing this business ------------------------------------------
    #
    # Which of the two you are offered is not a preference. A business that has
    # been audited holds findings, the reasoning behind them, and the log of
    # what the agent decided - and guardrail 5 promises every one of those is
    # replayable. A delete button that erased them on request would make that
    # promise conditional, so an audited business can only be put away.
    confirm_field = (
        '<label>Type the business name to confirm</label>'
        f'<input name="confirm" placeholder="{views.esc(biz_name)}" '
        'autocomplete="off" required>')

    if deletable:
        holds = (f"{sales} recorded sale{'' if sales == 1 else 's'} and its "
                 f"rate card" if sales
                 else "nothing but its name and a rate card")
        removal = f"""
<div class="card">
  <h2>Remove this business</h2>
  <p class="sub" style="margin:3px 0 12px">This business has never been
     audited, so no finding, no agent decision and no dispute letter exists for
     it. Deleting it removes {views.esc(holds)}, along with who could see it.
     It cannot be undone.</p>
  <form method="post" action="/settings/delete">
    <div class="row">
      <div>{confirm_field}</div>
      <div style="flex:0"><button class="ghost">Delete business</button></div>
    </div>
  </form>
</div>"""
    else:
        removal = f"""
<div class="card">
  <h2>Remove this business</h2>
  <p class="sub" style="margin:3px 0 12px">This business has been audited
     {settled} time{'' if settled == 1 else 's'}, so it can be archived but not
     deleted. Archiving takes it out of the switcher and closes it to new
     sales. Its settlements, findings and the record of what the agent decided
     stay exactly as they are, and you can restore it at any time.</p>
  <form method="post" action="/settings/archive">
    <div class="row">
      <div>{confirm_field}</div>
      <div style="flex:0"><button class="ghost">Archive business</button></div>
    </div>
  </form>
  <p class="sub" style="margin:11px 0 0;font-size:11.5px">Every finding this
     product raises is an accusation that somebody was overcharged. Deleting
     the evidence on request would make the audit trail worth less than the
     accusation.</p>
</div>"""

    body = f"""
{banner}
<h1>Settings</h1>
<p class="sub">{views.esc(shell["business"]["name"])}</p>

<div class="card flush">
  <div class="card-head">
    <div><h2>Your rate card</h2>
      <p class="sub" style="margin:2px 0 0">What you agreed to pay. Every
        deduction is checked against these, so they should match your real
        contract.</p></div>
  </div>
  <table>
    <tr><th>Instrument</th>
        <th class="r">Network MDR % &nbsp;&nbsp; Platform %</th>
        <th class="r">Effective</th>
        <th class="r">On a {rupees(SAMPLE)} sale</th></tr>
    {''.join(rows)}
  </table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    &#128274; Rates set by RBI or by statute are shown but cannot be raised. A
    contract cannot agree to more than the law allows &mdash; and if one could
    be edited upward here, the auditor would stop reporting those violations
    entirely, because it would have been told they are contractual.
  </div>
</div>

<div class="split">
  <div class="card">
    <h2>Tax and tolerance</h2>
    <p class="sub" style="margin:3px 0 12px">Applied to every check.</p>
    <div class="money">
      <div class="lbl">GST on the fee</div>
      <div class="val">{card["gst_rate_bps"] / 100:.0f}%</div>
      <div class="lbl">Ignore gaps below</div>
      <div class="val">{rupees(card["tolerance"]["floor_paise"])} or
        {card["tolerance"]["pct_bps"] / 100:.2f}%</div>
    </div>
    <p class="sub" style="margin:11px 0 0;font-size:11.5px">Too tight and the
       report drowns in rounding noise; too loose and real overcharges slip
       through. GST is fixed by law at 18% of the fee, never of the sale.</p>
  </div>

  <div class="card">
    <h2>What the agent may close by itself</h2>
    <p class="sub" style="margin:3px 0 12px">Anything past these goes to a
       human instead.</p>
    <form method="post" action="/settings/guardrails">
      <div class="row">
        <div><label>Minimum confidence</label>
          <input name="min_confidence" type="number" step="0.05" min="0" max="1"
            value="{card["guardrails"]["min_confidence"]:.2f}"></div>
        <div><label>Always review above (&#8377;)</label>
          <input name="review_above" type="number" step="1" min="0"
            value="{card["guardrails"]["review_above_paise"] // 100}"></div>
        <div style="flex:0"><button class="ghost">Save</button></div>
      </div>
    </form>
    <p class="sub" style="margin:11px 0 0;font-size:11.5px">Sending a correct
       finding to a person costs two minutes. Auto-closing a wrong one costs
       money nobody ever learns about. These are not symmetrical.</p>
  </div>
</div>

{removal}
"""
    return views.page("Settings", body, "settings", **shell)


def _agent_block(agent_id: str, spec: dict, row, history,
                 have_key: bool) -> str:
    """
    One agent's measured accuracy, its controls, and its run history.

    Kept per agent rather than pooled. Two agents measured on two different
    kinds of batch do not average into a meaningful number, and a single
    headline covering both would be the sort of figure that falls apart the
    moment somebody asks what it is the accuracy OF.
    """
    from merchant.benchmark import has_recording

    return f"""
<div style="margin:26px 0 10px">
  <h2 style="margin:0">{views.esc(spec["name"])}</h2>
  <p class="sub" style="margin:2px 0 0">{views.esc(spec["measures"])}</p>
</div>

{_scorecard_card(row)}

{_benchmark_controls(agent_id, has_recording(agent_id), have_key)}

{_benchmark_history(history)}"""


def _scorecard_card(row) -> str:
    """The headline. Empty until somebody has actually measured something."""
    import json

    from merchant.benchmark import FREE_MODES, MODE_LABEL

    if row is None:
        return """
<div class="card">
  <div class="empty" style="padding:34px 16px">
    <div style="font-weight:560;color:var(--ink);margin-bottom:4px">
      Nothing measured yet</div>
    Run it below. Once a live run has happened it records itself,
    and replaying that recording afterwards is free.
  </div>
</div>"""

    detail = json.loads(row["detail"] or "{}")
    accuracy = row["correct"] / row["total"] if row["total"] else 0
    recall = (row["anomalies_caught"] / row["anomalies"]
              if row["anomalies"] else 0)
    decoy = (row["decoys_dismissed"] / row["decoys"] if row["decoys"] else 0)
    agent_acc = (row["by_agent_correct"] / row["by_agent"]
                 if row["by_agent"] else 0)

    # An outage is not a score. Same rule the terminal scorecard follows: if
    # calls failed, those records were escalated rather than judged, and a
    # percentage computed over them is measuring the network.
    if row["failed_calls"]:
        headline = f"""
<div class="banner warn"><span><b>{row["failed_calls"]} of {row["by_agent"]}
  classifications failed to run.</b> Those records were escalated, not judged.
  This run measured an outage rather than the system &mdash; do not quote
  it.</span></div>"""
    else:
        headline = ""

    accused = detail.get("false_accusations") or []
    missed = detail.get("misses") or []

    def wrong_list(title, rows, blurb):
        if not rows:
            return ""
        items = "".join(
            f"<tr><td class=\"mono\">{views.esc(str(r[0]))}</td>"
            f"<td>was {views.esc(str(r[1]))}</td>"
            f"<td>called it {views.esc(str(r[2]))}</td></tr>" for r in rows)
        return f"""
<div class="card flush">
  <div class="card-head"><h2>{title}</h2></div>
  <table>{items}</table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">{blurb}</div>
</div>"""

    return f"""
{headline}
<div class="card" style="padding:0;overflow:hidden">
  <div class="stats">
    <div class="stat"><b>{accuracy:.1%}</b><span>match rate
      ({row["correct"]}/{row["total"]})</span></div>
    <div class="stat"><b>{recall:.0%}</b><span>anomalies caught
      ({row["anomalies_caught"]}/{row["anomalies"]})</span></div>
    <div class="stat"><b>{decoy:.0%}</b><span>decoys correctly dismissed
      ({row["decoys_dismissed"]}/{row["decoys"]})</span></div>
    <div class="stat"><b>{row["false_accusations"]}</b>
      <span>false accusations</span></div>
    <div class="stat"><b>{rupees(row["recoverable_paise"])}</b>
      <span>recoverable identified</span></div>
  </div>
</div>

<p class="sub" style="margin:-4px 0 18px;font-size:11.5px">
  {views.esc(MODE_LABEL.get(row["mode"], row["mode"]))} &middot;
  {row["n_records"]} records, seed {row["seed"]} &middot;
  {row["duration_ms"] / 1000:.1f}s &middot;
  {views.when(row["at"])}
  {"&middot; no API calls, no cost" if row["mode"] in FREE_MODES else ""}
</p>

<div class="split">
  <div class="card flush">
    <div class="card-head"><h2>Who decided what</h2></div>
    <table>
      <tr><th>Decided by</th><th class="r">Records</th><th class="r">Correct</th>
          <th class="r">Rate</th></tr>
      <tr><td>The rate card, mechanically</td>
        <td class="r">{row["by_calculator"]}</td>
        <td class="r">{row["by_calculator_correct"]}</td>
        <td class="r">{(row["by_calculator_correct"] / row["by_calculator"]
                        if row["by_calculator"] else 0):.0%}</td></tr>
      <tr><td>The agent, asked to judge</td>
        <td class="r">{row["by_agent"]}</td>
        <td class="r">{row["by_agent_correct"]}</td>
        <td class="r">{agent_acc:.0%}</td></tr>
    </table>
    <div style="padding:11px 16px;border-top:1px solid var(--line-2);
      color:var(--muted);font-size:11.5px">
      The split a judge asks about within thirty seconds. A headline that
      quietly includes records the model never saw is a flattering number
      unless you say so.
    </div>
  </div>
  <div class="card flush">
    <div class="card-head"><h2>What it did with them</h2></div>
    <table>
      <tr><td>Closed without a human</td>
        <td class="r">{row["auto_resolved"]}</td></tr>
      <tr><td>Sent to a person to decide</td>
        <td class="r">{row["queued_for_human"]}</td></tr>
      <tr><td>Clean records left alone</td>
        <td class="r">{row["clean_correct"]}/{row["clean"]}</td></tr>
      <tr><td>Anomalies it did not catch</td>
        <td class="r">{row["anomalies_missed"]}</td></tr>
    </table>
    <div style="padding:11px 16px;border-top:1px solid var(--line-2);
      color:var(--muted);font-size:11.5px">
      Three of the eleven exception codes mean &ldquo;do nothing&rdquo;.
      Knowing when not to alarm somebody is as much of the work as catching
      the overcharge.
    </div>
  </div>
</div>

{wrong_list("False accusations", accused,
            "Records it called an overcharge that were not one. This is the "
            "number that matters most - a tool that cries wolf gets switched "
            "off, and every finding here is an accusation against a named "
            "gateway.")}
{wrong_list("Anomalies it missed", missed,
            "Planted errors it did not find. Money the merchant would not "
            "have recovered.")}"""


def _benchmark_controls(agent_id: str, have_recording: bool,
                        have_key: bool) -> str:
    from merchant.benchmark import BENCHMARK_AGENTS, DEFAULT_N

    spec = BENCHMARK_AGENTS[agent_id]

    replay = ('<button class="ghost">Replay the recording</button>'
              if have_recording else
              '<span class="sub">Nothing recorded yet. Run the agent live '
              'once and it records itself, after which replaying is free.</span>')

    live = ('<button class="ghost">Run the agent live</button>' if have_key else
            '<span class="sub">No ANTHROPIC_API_KEY is set, so the agent '
            'cannot run.</span>')

    return f"""
<div class="split">
  <div class="card">
    <h2>Replay a recording</h2>
    <p class="sub" style="margin:3px 0 12px">Re-scores decisions the agent made
       earlier, against a freshly generated batch with the same seed. No API
       calls and no cost, so rehearse with it as often as you like.</p>
    <form method="post" action="/admin/accuracy/run">
      <input type="hidden" name="mode" value="replay">
      <input type="hidden" name="agent_id" value="{views.esc(agent_id)}">
      {replay}
    </form>
  </div>
  <div class="card">
    <h2>Run the agent live</h2>
    <p class="sub" style="margin:3px 0 12px">Generates {DEFAULT_N} records. The
       rate card settles most of them for nothing; about
       {spec["judgment_records"]} need judgment and are the only ones billed,
       so a run costs roughly &#8377;{spec["approx_rupees"]}. Use it to
       validate, not to rehearse.</p>
    <form method="post" action="/admin/accuracy/run">
      <input type="hidden" name="mode" value="live">
      <input type="hidden" name="agent_id" value="{views.esc(agent_id)}">
      {live}
    </form>
  </div>
</div>"""


def _benchmark_history(rows) -> str:
    from merchant.benchmark import MODE_LABEL

    if len(rows) < 2:
        return ""
    body = "".join(f"""
      <tr>
        <td class="r" style="color:var(--muted)">{views.when(r["at"])}</td>
        <td>{views.esc(MODE_LABEL.get(r["mode"], r["mode"]))}</td>
        <td class="r">{r["correct"]}/{r["total"]}</td>
        <td class="r">{(r["correct"] / r["total"] if r["total"] else 0):.1%}</td>
        <td class="r">{r["false_accusations"]}</td>
        <td class="r" style="color:var(--muted)">{r["duration_ms"] / 1000:.1f}s</td>
      </tr>""" for r in rows)
    return f"""
<div class="card flush">
  <div class="card-head"><h2>Every run, including the bad ones</h2></div>
  <table>
    <tr><th class="r">When</th><th>How</th><th class="r">Correct</th>
        <th class="r">Match rate</th><th class="r">False accusations</th>
        <th class="r">Took</th></tr>
    {body}
  </table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    Kept in full. A benchmark you can re-run until it flatters you is not a
    measurement, so every run is listed and none can be deleted here.
  </div>
</div>"""


# --- how accurate is the agent, measured -----------------------------------
#
# Operator-only, and not because it is sensitive. It is here because it is a
# fact about the agent rather than about anybody's money: the batch is
# synthetic, the errors are planted, and the answer key came from the
# generator. A merchant's real settlements have no answer key - which is the
# whole reason this product exists - so a match rate shown beside real findings
# would be a number somebody made up.

BENCH: dict = {}                        # benchmark_id -> Progress, while running
_bench_lock = threading.Lock()


def _bench_progress(key: str):
    def report(**kw):
        with _bench_lock:
            state = BENCH.get(key)
            if state is None:
                return
            for field, value in kw.items():
                setattr(state, field, value)
    return report


def _run_benchmark_thread(key: str, agent_id: str, mode: str, effort: str,
                          ran_by: str) -> None:
    from merchant.benchmark import Benchmarks, run_benchmark

    try:
        card, ms = run_benchmark(agent_id=agent_id, mode=mode, effort=effort,
                                 on_progress=_bench_progress(key))
        with ledger() as led:
            benchmark_id = Benchmarks(led.conn).record(
                card, agent_id=agent_id, mode=mode, n=card.total,
                seed=benchmark_mod.DEFAULT_SEED, model="opus", effort=effort,
                duration_ms=ms, ran_by=ran_by)
        with _bench_lock:
            state = BENCH.get(key)
            if state:
                state.state = "done"
                state.benchmark_id = benchmark_id
    except Exception as exc:            # noqa: BLE001 - shown to the operator
        with _bench_lock:
            state = BENCH.get(key)
            if state:
                state.state = "failed"
                state.error = str(exc)


@app.get("/admin/accuracy", response_class=HTMLResponse)
def accuracy_page(op: User = Depends(operator),
                  ws: Optional[Workspace] = Depends(workspace),
                  error: str = "", ok: str = ""):
    from merchant.benchmark import (BENCHMARK_AGENTS, Benchmarks, has_recording)

    with ledger(ws.business_id if ws else None) as led:
        shell = _shell_for(led, ws)
        bench = Benchmarks(led.conn)
        results = {a: bench.latest(a) for a in BENCHMARK_AGENTS}
        histories = {a: bench.history(12, a) for a in BENCHMARK_AGENTS}

    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    banner = ""
    if error:
        banner = f'<div class="banner warn"><span>{views.esc(error)}</span></div>'
    elif ok:
        banner = f'<div class="banner brand"><span>{views.esc(ok)}</span></div>'

    body = f"""
{banner}
<h1>Measured accuracy</h1>
<p class="sub">The agent against a batch whose answers are known in advance.
   Sixty records, errors planted deliberately, scored against the key the
   generator handed back.</p>

{"".join(_agent_block(agent_id, spec, results[agent_id],
                     histories[agent_id], have_key)
         for agent_id, spec in BENCHMARK_AGENTS.items())}

<div class="card tint">
  <h2>Why this number is not on the settlements page</h2>
  <p class="sub" style="margin:4px 0 0">A match rate needs something to be
     right <i>about</i>. This batch has an answer key because the generator
     planted the errors. A merchant&rsquo;s real settlements do not &mdash;
     nobody knows which of their deductions were wrong, which is the entire
     reason this product exists. A percentage printed beside real findings
     would be invented, so there is deliberately nowhere in the merchant-facing
     product that shows one.</p>
</div>"""
    return views.page("Measured accuracy", body, "accuracy", **shell)


@app.post("/admin/accuracy/run")
def run_benchmark_route(request: Request, mode: str = Form("replay"),
                        agent_id: str = Form("settlement_audit"),
                        effort: str = Form("medium"),
                        op: User = Depends(operator)):
    from urllib.parse import quote

    from merchant.benchmark import (BENCHMARK_AGENTS, MODE_LIVE, MODE_REPLAY,
                                    has_recording)
    from merchant.ratelimit import client_address

    if mode not in (MODE_REPLAY, MODE_LIVE):
        return RedirectResponse("/admin/accuracy", status_code=303)
    if agent_id not in BENCHMARK_AGENTS:
        return RedirectResponse("/admin/accuracy", status_code=303)

    if mode == MODE_REPLAY and not has_recording(agent_id):
        return RedirectResponse(
            f"/admin/accuracy?error={quote('Nothing has been recorded for that agent yet. Run it live once first.')}",
            status_code=303)

    if mode == MODE_LIVE and not os.environ.get("ANTHROPIC_API_KEY"):
        return RedirectResponse(
            f"/admin/accuracy?error={quote('No ANTHROPIC_API_KEY is set, so the agent cannot run. Replay a recording instead.')}",
            status_code=303)

    with ledger() as led:
        AccessLog(led.conn).record(
            Action.RUN_BENCHMARK, user=op, business_id=None,
            target=agent_id, address=client_address(request),
            detail=f"ran the {BENCHMARK_AGENTS[agent_id]['name']} "
                   f"benchmark ({mode})")

    key = f"bench_{int(time.time() * 1000)}"
    with _bench_lock:
        BENCH[key] = benchmark_mod.Progress(total=benchmark_mod.DEFAULT_N)
    threading.Thread(target=_run_benchmark_thread,
                     args=(key, agent_id, mode, effort, op.email),
                     daemon=True).start()
    return RedirectResponse(f"/admin/accuracy/running/{key}", status_code=303)


@app.get("/admin/accuracy/running/{key}", response_class=HTMLResponse)
def benchmark_running(key: str, op: User = Depends(operator),
                      ws: Optional[Workspace] = Depends(workspace)):
    with _bench_lock:
        state = BENCH.get(key)

    if state is None:
        return RedirectResponse("/admin/accuracy", status_code=303)
    if state.state == "done":
        return RedirectResponse("/admin/accuracy?ok=Benchmark+complete.",
                                status_code=303)
    if state.state == "failed":
        from urllib.parse import quote

        return RedirectResponse(f"/admin/accuracy?error={quote(state.error)}",
                                status_code=303)

    with ledger(ws.business_id if ws else None) as led:
        shell = _shell_for(led, ws)

    pct = int(100 * state.done / state.total) if state.total else 0
    body = f"""
<h1>Measuring</h1>
<p class="sub">{views.esc(state.phase or "Starting")}</p>
<div class="card">
  <div style="height:6px;background:var(--line-2);border-radius:3px;
    overflow:hidden">
    <div style="height:100%;width:{pct}%;background:var(--brand)"></div>
  </div>
  <p class="sub" style="margin:12px 0 0">{state.done} of {state.total}
     {views.esc(state.note)}</p>
</div>
<meta http-equiv="refresh" content="1">"""
    return views.page("Measuring", body, "accuracy", **shell)


# --- archiving and deleting a business ------------------------------------


def _confirm_matches(typed: str, name: str) -> bool:
    """Forgiving about case and stray spaces, strict about the actual name."""
    return typed.strip().casefold() == name.strip().casefold()


@app.post("/settings/archive")
def archive_business(request: Request, confirm: str = Form(""),
                     ws: Workspace = Depends(required_workspace)):
    ws.require_owner("whether this business stays open", request,
                     Action.ARCHIVE_BUSINESS)

    from urllib.parse import quote

    with ledger(ws.business_id) as led:
        name = led.businesses.get(ws.business_id)["name"]
        if not _confirm_matches(confirm, name):
            ws.audit(Action.ARCHIVE_BUSINESS, request, allowed=False,
                     detail="name typed to confirm did not match")
            return RedirectResponse(
                f"/settings?error={quote('That is not the name of this business. Nothing was changed.')}",
                status_code=303)
        led.businesses.archive(ws.business_id)

    ws.audit(Action.ARCHIVE_BUSINESS, request, target=ws.business_id,
             detail=f"archived {name}")
    response = RedirectResponse(
        f"/businesses?ok={quote(f'{name} is archived. Its books are unchanged and you can restore it here.')}",
        status_code=303)
    response.delete_cookie(COOKIE)      # you cannot stand inside an archived one
    return response


@app.post("/businesses/restore")
def restore_business(request: Request, business_id: str = Form(...),
                     user: User = Depends(current_user)):
    from urllib.parse import quote

    with ledger() as led:
        auth = Auth(led.conn)
        if auth.role_in(user, business_id) != Role.OWNER:
            AccessLog(led.conn).denied(
                Action.ARCHIVE_BUSINESS, user=user, business_id=business_id,
                detail="only an owner may restore a business")
            return RedirectResponse("/businesses", status_code=303)
        name = led.businesses.get(business_id)["name"]
        led.businesses.restore(business_id)
        AccessLog(led.conn).record(
            Action.ARCHIVE_BUSINESS, user=user, business_id=business_id,
            detail=f"restored {name}")
    return RedirectResponse(f"/businesses?ok={quote(f'{name} is open again.')}",
                            status_code=303)


@app.post("/settings/delete")
def delete_business(request: Request, confirm: str = Form(""),
                    ws: Workspace = Depends(required_workspace)):
    """
    Delete a business outright - only ever one that has never been audited.

    The refusal lives in Businesses.delete as well as here. This route checks
    first so it can offer archiving instead; the one underneath exists so that
    no future caller can destroy an audit trail by forgetting to ask.
    """
    ws.require_owner("whether this business stays open", request,
                     Action.DELETE_BUSINESS)

    from urllib.parse import quote

    with ledger(ws.business_id) as led:
        name = led.businesses.get(ws.business_id)["name"]

        if not led.businesses.may_delete(ws.business_id):
            ws.audit(Action.DELETE_BUSINESS, request, allowed=False,
                     detail="has settlements - archiving is the only option")
            return RedirectResponse(
                f"/settings?error={quote('This business has been audited. It can be archived, but its findings are not deletable.')}",
                status_code=303)

        if not _confirm_matches(confirm, name):
            ws.audit(Action.DELETE_BUSINESS, request, allowed=False,
                     detail="name typed to confirm did not match")
            return RedirectResponse(
                f"/settings?error={quote('That is not the name of this business. Nothing was deleted.')}",
                status_code=303)

        removed = led.businesses.delete(ws.business_id)

        # Deliberately NOT scoped to the business. Everything carrying its id
        # was just deleted, including its own access log - a record of the
        # deletion filed under the deleted thing deletes itself. This one is
        # filed against the person who did it, which is what survives and what
        # anybody asking "where did that business go" needs.
        from merchant.ratelimit import client_address

        AccessLog(led.conn).record(
            Action.DELETE_BUSINESS, user=ws.user, business_id=None,
            target=ws.business_id, address=client_address(request),
            detail=f"deleted {name} ("
                   + ", ".join(f"{v} {k}" for k, v in sorted(removed.items()))
                   + ")")

    response = RedirectResponse(
        f"/businesses?ok={quote(f'{name} is deleted.')}", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.post("/settings/rate")
def set_rate(instrument: str = Form(...), network_pct: str = Form("0"),
             platform_pct: str = Form("0"),
             ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id
    ws.require_owner("a rate on your contract")
    try:
        network_bps = int(round(float(network_pct) * 100))
        platform_bps = int(round(float(platform_pct) * 100))
    except (ValueError, TypeError):
        return RedirectResponse("/settings?error=Rates+must+be+numbers",
                                status_code=303)

    from urllib.parse import quote

    with ledger(resolved) as led:
        try:
            led.businesses.set_rate(resolved, instrument, network_bps, platform_bps)
        except (ValueError, KeyError) as exc:
            return RedirectResponse(f"/settings?error={quote(str(exc))}",
                                    status_code=303)
    return RedirectResponse(
        f"/settings?ok={quote(f'{instrument} is now {(network_bps + platform_bps) / 100:.2f}%')}",
        status_code=303)


@app.post("/settings/guardrails")
def set_guardrails(min_confidence: str = Form("0.75"),
                   review_above: str = Form("250"),
                   ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id
    ws.require_owner("the review thresholds")

    from urllib.parse import quote

    try:
        confidence = float(min_confidence)
        above = int(round(float(review_above) * 100))
    except (ValueError, TypeError):
        return RedirectResponse("/settings?error=Those+need+to+be+numbers",
                                status_code=303)

    with ledger(resolved) as led:
        try:
            led.businesses.set_guardrails(resolved, confidence, above)
        except ValueError as exc:
            return RedirectResponse(f"/settings?error={quote(str(exc))}",
                                    status_code=303)
    return RedirectResponse("/settings?ok=Review+thresholds+updated",
                            status_code=303)


@app.post("/settings/suppliers")
def set_supplier_behaviour(behaviour: str = Form(...),
                           ws: Workspace = Depends(required_workspace)):
    """A demo control, so it lives with the other demo controls."""
    from merchant.suppliers import SupplierBehaviour

    try:
        chosen = SupplierBehaviour(behaviour)
    except ValueError:
        chosen = SupplierBehaviour.CORRECT
    with ledger(ws.business_id) as led:
        led.businesses.set_supplier_behaviour(ws.business_id, str(chosen))
    return RedirectResponse("/data/simulator", status_code=303)


@app.post("/settings/gateway")
def set_gateway(behaviour: str = Form(...),
                ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id
    ws.require_owner("the gateway simulator")
    if resolved:
        with ledger(resolved) as led:
            led.set_behaviour(Behaviour(behaviour))
    return RedirectResponse("/data/simulator", status_code=303)


# --- ask the auditor ------------------------------------------------------

# Recent questions, per business, in memory. Deliberately not persisted: a
# question is a conversation, not a record, and storing merchants' questions is
# a data-retention decision nobody has made yet.
ASKED: dict[str, list] = {}


@app.get("/agents/settlement/ask", response_class=HTMLResponse)
def ask_page(ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id

    from merchant.ask import SUGGESTIONS

    with ledger(resolved) as led:
        shell = _shell_for(led, ws)
        _head = _workspace_head(led, ws, "settlement_audit", "ask")
        _needs = _setup_banner(_requirements(led, ws, "settlement_audit"))
        runs = led.settlements()
        audited = [r for r in runs if r["findings"]]
        findings = sum(led.store.totals(r["run_id"])["n"] for r in audited)
        actionable = sum(
            1 for r in audited for f in led.store.findings(r["run_id"])
            if f["exception_code"] != "CLEAN")
        card = led.rate_card()

    history = ASKED.get(resolved, [])

    chips = "".join(f'<button type="button" class="chip" data-q="{views.esc(q)}">'
                    f'{views.esc(q)}</button>' for q in SUGGESTIONS)

    answers = "".join(_answer_html(a) for a in reversed(history))

    if not audited:
        body = f"""
{_head}
{_needs}
<p class="sub">It answers from your books and nothing else &mdash; so there has
   to be something in them first.</p>
<div class="card">{views.checklist([
    ("done", "Business set up", shell["business"]["name"], "", ""),
    ("now", "Audit a settlement",
     "The agent can only answer from findings that exist. Nothing has been "
     "audited yet, so there is nothing for it to read.",
     "Go to settlements", "/settlements"),
    ("later", "Then ask it anything",
     "Why a payout was short, how much is recoverable, which findings are "
     "worth arguing about.", "", ""),
])}</div>"""
        return views.page("Ask", body, "agent:settlement_audit", **shell)

    body = f"""
{_head}
{_needs}
<p class="sub">Every figure it quotes was computed by the engine, and the answer
   is checked before you see it.</p>

<div class="card">
  <form method="post" action="/ask" id="ask-form">
    <div class="row">
      <div><input name="question" id="ask-input" maxlength="500" required
        autocomplete="off" placeholder="Why was my last payout short?"></div>
      <div style="flex:0"><button id="ask-go">Ask</button></div>
    </div>
  </form>
  <div style="margin-top:13px">{chips}</div>
  <div class="scope" style="margin-top:14px;padding-top:12px;
    border-top:1px solid var(--line-2)">
    <span>It can see <b>{len(audited)}</b> audited settlement(s)</span>
    <span><b>{findings}</b> records, <b>{actionable}</b> with findings</span>
    <span>your rate card (<b>{len(card["instruments"])}</b> instruments)</span>
    <span style="color:var(--faint)">and nothing else</span>
  </div>
</div>

<div id="ask-thread">{answers}</div>

<script>
(function () {{
  var form = document.getElementById('ask-form');
  var input = document.getElementById('ask-input');
  var go = document.getElementById('ask-go');
  var thread = document.getElementById('ask-thread');

  document.querySelectorAll('.chip').forEach(function (chip) {{
    chip.onclick = function () {{ input.value = chip.dataset.q; submit(); }};
  }});

  function block(cls, html) {{
    var el = document.createElement('div');
    el.className = cls;
    el.innerHTML = html;
    return el;
  }}

  function submit(ev) {{
    if (ev) ev.preventDefault();
    var question = input.value.trim();
    if (!question) return;

    go.disabled = true;
    input.value = '';

    // The answer takes ten to fifteen seconds. A form POST would leave the
    // page blank for all of it; this shows the question landing immediately
    // and something moving while the agent reads.
    var pending = block('qa', '');
    var q = block('qa-q', '<span class="qa-who">You</span>' +
                          '<span class="qa-text"></span>');
    q.querySelector('.qa-text').textContent = question;
    var a = block('qa-a', '<span class="qa-thinking">Reading your settlements' +
                          '<span class="qa-dots"></span></span>');
    pending.appendChild(q); pending.appendChild(a);
    thread.insertBefore(pending, thread.firstChild);

    fetch('/ask', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json'}},
      body: 'question=' + encodeURIComponent(question)
    }})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{ pending.outerHTML = d.html; }})
      .catch(function () {{
        a.innerHTML = '<p style="color:var(--danger)">Could not reach the ' +
                      'agent. The answer was not lost - try again.</p>';
      }})
      .finally(function () {{ go.disabled = false; input.focus(); }});
  }}

  form.onsubmit = submit;
}})();
</script>"""
    return views.page("Ask", body, "agent:settlement_audit", **shell)


def _answer_html(answer) -> str:
    """One exchange. Rendered the same whether it arrived by fetch or by form."""
    warning = ""
    if answer.invented_figures:
        warning = (f'<div class="banner warn" style="margin:0 0 11px">'
                   f'<b>Check this</b><span>It stated figures that were not in '
                   f'its briefing ({views.esc(", ".join(answer.invented_figures))}). '
                   f'Treat those numbers as unverified.</span></div>')

    text = answer.text or answer.error or ""
    paragraphs = "".join(f"<p>{views.esc(p.strip())}</p>"
                         for p in text.split("\n") if p.strip())

    meta = []
    if answer.error:
        meta.append("could not answer")
    else:
        meta.append(f"answered in {round(answer.latency_ms / 1000)}s")
        meta.append("every figure checked against your own data")
        if answer.output_tokens:
            meta.append(f"{answer.output_tokens} tokens")

    return f"""
    <div class="qa">
      <div class="qa-q"><span class="qa-who">You</span>
        <span class="qa-text">{views.esc(answer.question)}</span></div>
      <div class="qa-a">
        {warning}{paragraphs}
        <div class="qa-meta">{
          "".join(f"<span>{views.esc(m)}</span>" for m in meta)}</div>
      </div>
    </div>"""


@app.post("/ask")
def submit_question(request: Request, question: str = Form(...),
                    ws: Workspace = Depends(required_workspace)):
    from merchant.ratelimit import client_address

    resolved = ws.business_id

    from merchant.ask import ask

    with ledger(resolved) as led:
        answer = ask(led, question)
        # The agent reads this business's books to answer. That is access.
        AccessLog(led.conn).record(
            Action.ASK_AGENT, user=ws.user, business_id=resolved,
            address=client_address(request), detail=question[:120])

    history = ASKED.setdefault(resolved, [])
    history.append(answer)
    del history[:-8]                     # keep the page readable

    # Answered as JSON for the page's own fetch, and as a redirect for anyone
    # without JavaScript. Same renderer either way, so the two cannot diverge.
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"html": _answer_html(answer),
                             "trustworthy": answer.trustworthy})
    return RedirectResponse("/agents/settlement/ask", status_code=303)


# --- settlements ----------------------------------------------------------

@app.post("/settle")
def settle(ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id
    with ledger(resolved) as led:
        batch = led.build_settlement(led.rate_card())
        if batch is None:
            return RedirectResponse("/data/simulator", status_code=303)
        run_id = led.commit_settlement(batch)
    return RedirectResponse(f"/settlements/{run_id}", status_code=303)


@app.get("/agents/settlement", response_class=HTMLResponse)
def settlements_page(ws: Workspace = Depends(required_workspace)):
    resolved = ws.business_id

    with ledger(resolved) as led:
        shell = _shell_for(led, ws)
        _head = _workspace_head(led, ws, "settlement_audit", "")
        _needs = _setup_banner(_requirements(led, ws, "settlement_audit"))
        runs = led.settlements()
        detail = {}
        for r in runs:
            money = led.conn.execute(
                "SELECT COALESCE(SUM(amount),0) g, COALESCE(SUM(fee),0) f,"
                " COALESCE(SUM(tax),0) t FROM settlement_lines"
                " WHERE run_id = ? AND type = 'payment'", (r["run_id"],)).fetchone()
            credited = led.conn.execute(
                "SELECT COALESCE(SUM(amount),0) a FROM bank_credits"
                " WHERE run_id = ?", (r["run_id"],)).fetchone()["a"]
            detail[r["run_id"]] = {
                "gross": money["g"], "deducted": money["f"] + money["t"],
                "credited": credited, **led.store.totals(r["run_id"])}

    from merchant.sources import SourceKind

    simulated = shell.get("source") == str(SourceKind.SIMULATOR)
    empty_href = "/data/simulator" if simulated else "/data"
    empty_label = "Take a payment" if simulated else "Pull your settlements"

    grand = {k: sum(d[k] for d in detail.values())
             for k in ("gross", "deducted", "recoverable_paise", "queued")} \
        if detail else {"gross": 0, "deducted": 0, "recoverable_paise": 0,
                        "queued": 0}

    rows = "".join(f"""
      <tr>
        <td class="mono"><a href="/agents/settlement/run/{views.esc(r["run_id"])}"
          style="color:var(--brand-ink)">{views.esc(r["run_id"])}</a></td>
        <td class="r">{views.when(r["created_at"])}</td>
        <td class="r">{r["n_records"]}</td>
        <td class="r">{rupees(detail[r["run_id"]]["gross"])}</td>
        <td class="r">{rupees(detail[r["run_id"]]["deducted"])}</td>
        <td class="r">{rupees(detail[r["run_id"]]["credited"])}</td>
        <td class="r">{
          f'<span class="pill danger">{rupees(detail[r["run_id"]]["recoverable_paise"])}</span>'
          if detail[r["run_id"]]["recoverable_paise"] else
          ('<span class="pill good">clean</span>' if r["findings"]
           else '<span class="pill">not audited</span>')}</td>
        <td class="r">{detail[r["run_id"]]["queued"] or "-"}</td>
      </tr>""" for r in runs)

    body = f"""
{_head}
{_needs}
<p class="sub">Each batch is what the gateway actually paid out, and what it
   deducted to get there. {len(runs)} batch(es) &middot;
   {rupees(grand["gross"])} gross &middot; {rupees(grand["deducted"])} deducted
   &middot; {rupees(grand["recoverable_paise"])} recoverable.</p>
<div class="card flush"><table>
  <tr><th>Settlement</th><th class="r">When</th><th class="r">Pmts</th>
      <th class="r">Gross</th><th class="r">Deducted</th><th class="r">Credited</th>
      <th class="r">Recoverable</th><th class="r">Queued</th></tr>
  {rows or f'<tr><td colspan="8" class="empty">'
           f'<div style="font-weight:560;color:var(--ink);margin-bottom:4px">'
           f'No settlements yet</div>'
           f'A settlement is one batch of payments the gateway paid out '
           f'together. Each one gets audited line by line.<br>'
           f'<a class="btn" style="margin-top:11px" href="{empty_href}">'
           f'{empty_label}</a></td></tr>'}
</table></div>"""
    return views.page("Settlements", body, "agent:settlement_audit", **shell)


def _progress(run_id: str):
    """
    Collects whatever an agent reports.

    `result=` is special: it appends rather than overwrites, so verdicts stream
    onto the page as they land instead of appearing all at once at the end.
    Everything else is a plain field update.
    """
    def update(**fields):
        with _lock:
            state = RUNS.setdefault(run_id, {})
            landed = fields.pop("result", None)
            spoken = fields.pop("line", None)
            state.update(fields)
            if landed is not None:
                state.setdefault("results", []).append(landed)
            if spoken is not None:
                state.setdefault("lines", []).append(spoken)
    return update


def _run_agent(agent_id: str, ctx: AgentContext, runner=None) -> None:
    """
    Run an agent in the background, catching everything.

    A thread that dies silently leaves the page polling forever, which looks
    exactly like a hang - the worst possible failure in front of an audience.

    `runner` overrides the catalogue lookup, for work belonging to a registered
    agent that is not the thing that agent's entry describes. The supplier
    watch is the reconciler doing a different job on the same data; giving it
    its own catalogue entry would inflate the agent count without adding a
    capability, which is what the catalogue docstring exists to prevent.
    """
    state = RUNS[ctx.target_id]
    try:
        (runner or catalog.get(agent_id).runner)(ctx)
        state["state"] = "done"
        state["phase"] = "done"
    except Exception as exc:                                # noqa: BLE001
        state["state"] = "error"
        state["phase"] = f"{type(exc).__name__}: {exc}"


@app.post("/audit/{run_id}")
def start_audit(run_id: str, use_agent: str = Form("no"),
                ws: Workspace = Depends(required_workspace)):
    # Default "no", not "yes". An unchecked HTML checkbox submits NOTHING, so a
    # default of "yes" meant unticking the box still ran the agent - the one
    # setting whose whole purpose is to not need a network or spend money.
    resolved = ws.business_id

    with ledger(resolved) as led:
        if not led.owns_run(run_id):
            return HTMLResponse(views.error_page(
                "Not this business's settlement",
                "That settlement belongs to a different business.",
                "Back", "/settlements"), status_code=404)
        card = led.rate_card()

    spec = catalog.get("settlement_audit")
    with _lock:
        existing = RUNS.get(run_id)
        if existing and existing.get("state") == "running":
            return RedirectResponse(f"/settlements/{run_id}", status_code=303)
        RUNS[run_id] = {"state": "running", "phase": "Starting", "done": 0,
                        "total": 0, "settled_by_rules": 0, "current": "",
                        "note": "", "lines": [], "results": [],
                        "agent": spec.name, "started": time.time()}

    with ledger(resolved) as led:
        AccessLog(led.conn).record(
            Action.RUN_AUDIT, user=ws.user, business_id=resolved,
            target=run_id, address="")

    ctx = AgentContext(business_id=resolved, rate_card=card, db=DB,
                       target_id=run_id, use_agent=(use_agent == "yes"),
                       progress=_progress(run_id))
    threading.Thread(target=_run_agent, args=("settlement_audit", ctx),
                     daemon=True).start()
    return RedirectResponse(f"/settlements/{run_id}", status_code=303)


@app.get("/audit/{run_id}/status")
def audit_status(run_id: str):
    state = RUNS.get(run_id)
    if state is None:
        return JSONResponse({"state": "idle"})
    return JSONResponse({**state,
                         "elapsed": round(time.time() - state["started"])})


# What each settlement finding means, said the way a merchant would say it.
#
# The page used to print ZERO_MDR_VIOLATION in red capitals. That is our enum,
# not their language - and the one word in it a merchant recognises, "violation",
# is the one that tells them least about what to do.
SETTLEMENT_ISSUE = {
    "CLEAN": "Correct",
    "ROUNDING": "Rounding",
    "ZERO_MDR_VIOLATION": "Charged a fee that is banned",
    "INSTRUMENT_MISLABEL": "Payment recorded as the wrong type",
    "RATE_MISMATCH": "Charged above your contract",
    "GST_MISMATCH": "GST worked out wrongly",
    "REFUND_MDR_RETAINED": "Fee kept on a refund",
    "PERIOD_BOUNDARY": "Belongs to another month",
    "TDS_CODE_MISMATCH": "Old tax code used",
    "MISSING_FROM_SETTLEMENT": "Never paid out",
    "UNEXPLAINED": "Needs a person",
}

SETTLEMENT_EXPLAIN = {
    "ZERO_MDR_VIOLATION": "UPI and RuPay carry no network fee by law. Your "
                          "gateway charged one anyway.",
    "INSTRUMENT_MISLABEL": "This was a UPI payment recorded as a card, so it "
                           "was priced at the card rate.",
    "RATE_MISMATCH": "The rate charged is higher than the one your contract "
                     "sets for this instrument.",
    "GST_MISMATCH": "GST is 18% of the fee. This was worked out on something "
                    "else.",
    "REFUND_MDR_RETAINED": "The sale was refunded and the fee was kept. That "
                           "is how every Indian gateway works - it is a cost, "
                           "not an error.",
    "PERIOD_BOUNDARY": "Sold in one month, settled in the next. Nothing is "
                       "wrong; it just falls in a different set of books.",
    "TDS_CODE_MISMATCH": "Tax was deducted under a section that stopped "
                         "existing on 1 April 2026.",
    "MISSING_FROM_SETTLEMENT": "This sale is in your books and no settlement "
                               "line pays it out.",
    "ROUNDING": "Off by less than the tolerance. Arithmetic noise, not a "
                "wrong rate.",
    "CLEAN": "Charged exactly what your contract says.",
    "UNEXPLAINED": "The evidence fitted nothing cleanly, so it was left for a "
                   "person rather than guessed at.",
}

SETTLEMENT_RECOMMEND = {
    "dispute": "Dispute this with your gateway.",
    "fix_books": "Correct your books.",
    "dismiss": "Nothing to do.",
    "escalate": "Have someone check this before you dispute it.",
}


def _settlement_card(f, instrument: str) -> str:
    """One finding, laid out the way the input credit findings are."""
    import json as _json

    code = f["exception_code"]
    action = f["action"] or "dismiss"
    stake = rupees(f["money_at_stake"] or 0)

    held = ""
    if f["queued_for_human"]:
        reasons = "; ".join(_json.loads(f["queue_reasons"] or "[]"))
        held = (f'<div class="held">Held for you to decide &mdash; '
                f'{views.esc(reasons)}</div>')

    dispute = ""
    if f["dispute_text"]:
        dispute = (
            '<details class="working" style="border-top:0;padding-top:9px">'
            '<summary>Read the dispute we drafted</summary>'
            f'<div class="draft">{views.esc(f["dispute_text"])}</div>'
            '</details>')

    confidence = ("arithmetic, not judgment"
                  if f["decided_by"] == "calculator"
                  else f'the agent at {(f["confidence"] or 0):.0%} confidence')

    return f"""
<div class="finding-card">
  <div class="finding-card-top">
    <div>
      <div class="finding-card-who">{views.esc(instrument)}</div>
      <div class="finding-card-inv mono">{views.esc(f["payment_id"])}</div>
    </div>
    {ui.code_badge(code, SETTLEMENT_ISSUE.get(code, ""))}
  </div>

  <p class="finding-card-why">{views.esc(SETTLEMENT_EXPLAIN.get(code, ""))}</p>

  <div class="facts">
    <div class="fact"><span>At stake</span>
      <b style="{"color:var(--danger)" if f["money_at_stake"] else ""}">{stake}</b></div>
    <div class="fact"><span>Fee charged</span>
      <b>{rupees(f["actual_fee"])}</b></div>
    <div class="fact"><span>Should have been</span>
      <b>{rupees(f["expected_fee"])}</b></div>
    <div class="fact"><span>GST charged</span>
      <b>{rupees(f["actual_tax"])}</b></div>
  </div>

  <div class="recommend">
    <span class="recommend-label">Recommended</span>
    {views.esc(SETTLEMENT_RECOMMEND.get(action, action))}
  </div>
  {held}
  {dispute}

  <details class="working">
    <summary>Show the working</summary>
    <div class="working-body">
      <div class="working-line"><span>Fee your gateway charged</span>
        <b>{rupees(f["actual_fee"])}</b></div>
      <div class="working-line"><span>Fee your contract allows</span>
        <b>{rupees(f["expected_fee"])}</b></div>
      <div class="working-line"><span>GST charged on it</span>
        <b>{rupees(f["actual_tax"])}</b></div>
      <div class="working-line"><span>GST that should have been</span>
        <b>{rupees(f["expected_tax"])}</b></div>
      <div class="working-line"><span>Difference</span>
        <b>{rupees(f["delta"])}</b></div>
      <p style="margin:12px 0 0;font-size:12.6px;color:var(--ink-2)">
        {views.esc(f["reasoning"] or "")}</p>
      <p style="margin:9px 0 0;font-size:11.5px;color:var(--muted)">
        Decided by {views.esc(confidence)}.
        {views.esc(f["rule_cited"] or "")}</p>
    </div>
  </details>
</div>"""


@app.get("/agents/settlement/run/{run_id}", response_class=HTMLResponse)
def settlement_page(run_id: str, request: Request,
                    ws: Workspace = Depends(required_workspace)):
    from merchant.ratelimit import client_address

    resolved = ws.business_id

    with ledger(resolved) as led:
        shell = _shell_for(led, ws)
        if not led.owns_run(run_id):
            AccessLog(led.conn).denied(
                Action.VIEW_SETTLEMENT, user=ws.user,
                business_id=ws.business_id, target=run_id,
                address=client_address(request),
                # True whether the settlement belongs to someone else or does
                # not exist at all. The page deliberately does not distinguish
                # the two - that would let anyone enumerate settlement ids -
                # and an audit log must not record a reason that might be false.
                detail="not reachable from this business")
            return HTMLResponse(views.error_page(
                "No such settlement here",
                "That settlement does not belong to this business.",
                "Back to settlements", "/settlements"), status_code=404)

        run = led.conn.execute("SELECT * FROM runs WHERE run_id = ?",
                               (run_id,)).fetchone()
        AccessLog(led.conn).record(
            Action.VIEW_SETTLEMENT, user=ws.user, business_id=ws.business_id,
            target=run_id, address=client_address(request))
        findings = led.store.findings(run_id)
        totals = led.store.totals(run_id)
        payments = {r["payment_id"]: r for r in led.conn.execute(
            "SELECT * FROM payments WHERE run_id = ?", (run_id,))}
        lines = led.conn.execute(
            "SELECT type, SUM(amount) a, SUM(fee) f, SUM(tax) t"
            " FROM settlement_lines WHERE run_id = ? GROUP BY type",
            (run_id,)).fetchall()
        credited = led.conn.execute(
            "SELECT COALESCE(SUM(amount),0) a FROM bank_credits WHERE run_id = ?",
            (run_id,)).fetchone()["a"]
        # This business's own contract - there is no global rate card any more,
        # and labelling an instrument against someone else's would be wrong in
        # exactly the way this product exists to catch.
        card = led.rate_card()

    gross = sum(p["amount"] for p in payments.values())
    fee_total = sum(r["f"] or 0 for r in lines)
    tax_total = sum(r["t"] or 0 for r in lines)
    refunded = -sum(r["a"] for r in lines if r["type"] == "refund")

    money = [("Gross sales", gross)]
    if refunded:
        money.append(("Refunded to customers", -refunded))
    money += [("Gateway fees", -fee_total), ("GST on fees", -tax_total)]
    ties = gross - refunded - fee_total - tax_total == credited

    state = RUNS.get(run_id, {"state": "idle"})
    audited = bool(findings)
    note = state.get("note", "")

    if state.get("state") == "running":
        # The live view and the replay are the same terminal, fed by the same
        # line builders. What a person watches during a run is exactly what
        # they read afterwards - there is no second, prettier narration.
        control = f"""
      <div class="card flush">
        <div class="card-head">
          <div><h2>{views.esc(state.get("agent", "Agent"))}</h2>
            <p class="sub" style="margin:2px 0 0" id="at-phase">
              Everything it thinks and does, as it happens.</p></div>
          <span class="sp"></span>
          <span class="pill brand" id="at-clock">0s</span>
        </div>
        <div style="padding:13px 16px">
          {views.terminal([], status="running", live=True)}
        </div>
        <div style="padding:0 16px 14px"><div class="progress">
          <div id="at-bar"></div></div></div>
      </div>
      <script>
      (function () {{
        var seen = 0;
        var PROMPT = {{ok:'\u2713', tool:'\u21b3', note:'!', fail:'\u2717'}};
        function el(id) {{ return document.getElementById(id); }}

        function draw(lines) {{
          var body = el('at-body');
          var cursor = body.lastElementChild;
          for (; seen < lines.length; seen++) {{
            var l = lines[seen];
            var row = document.createElement('div');
            row.className = 'at-ln ' + l.kind;
            row.innerHTML =
              '<span class="at-t"></span><span class="at-p"></span>' +
              '<span class="at-x"></span>';
            row.children[0].textContent = l.at || '';
            row.children[1].textContent = PROMPT[l.kind] || '>';
            row.children[2].textContent = l.text;
            body.insertBefore(row, cursor);
          }}
          body.scrollTop = body.scrollHeight;
        }}

        function poll() {{
          fetch('/audit/{run_id}/status').then(function (r) {{ return r.json(); }})
            .then(function (s) {{
              draw(s.lines || []);
              el('at-clock').textContent = (s.elapsed || 0) + 's';
              el('at-bar').style.width =
                (s.total ? (s.done / s.total * 100) : 6) + '%';
              if (s.state === 'done') {{ setTimeout(function () {{
                location.reload(); }}, 700); return; }}
              if (s.state === 'error') {{
                document.querySelector('.at-status').textContent = 'FAILED';
                document.querySelector('.at-status').className =
                  'at-status failed';
                el('at-phase').textContent = s.phase;
                return;
              }}
              setTimeout(poll, 800);
            }})
            .catch(function () {{ setTimeout(poll, 2000); }});
        }}
        poll();
      }})();
      </script>"""
    elif not audited:
        control = f"""
      <div class="card">
        <h2>Not audited yet</h2>
        <p class="sub">The rate card settles most records instantly. Anything
           needing judgment goes to the agent, about 15 seconds each.</p>
        <form method="post" action="/audit/{views.esc(run_id)}">
          <button>Run the settlement auditor</button>
          <label style="display:inline-flex;align-items:center;gap:6px;
            margin-left:14px;font-size:12.5px">
            <input type="checkbox" name="use_agent" value="yes" checked
              style="width:auto"> use the agent
          </label>
        </form>
      </div>"""
    else:
        control = ""

    from engine.taxonomy import RECOVERABLE

    recoverable_codes = {str(c) for c in RECOVERABLE}
    # Action drives the colour, not the exception code - the taxonomy is
    # organised by what the merchant must DO, and the interface should be too.
    colour = {"dispute": "var(--danger)", "fix_books": "var(--warn)",
              "escalate": "var(--violet)", "dismiss": "var(--muted)"}
    pill_class = {"dispute": "danger", "fix_books": "warn",
                  "escalate": "violet", "dismiss": ""}
    from engine.expected_value import Payment, classify_instrument

    def _instrument(pid: str) -> str:
        row = payments.get(pid)
        if row is None:
            return ""
        key, _ = classify_instrument(Payment(
            payment_id=pid, amount=row["amount"], method=row["method"],
            card_network=row["card_network"], card_type=row["card_type"],
            is_international=bool(row["is_international"]),
            upi_reference=row["upi_reference"]))
        return card["instruments"][key]["label"]

    cards = [_settlement_card(f, _instrument(f["payment_id"]))
             for f in findings
             if f["exception_code"] not in {"CLEAN", "ROUNDING"}]
    quiet = len(findings) - len(cards)

    # --- what the agent actually did ---------------------------------------
    trace_panel = ""
    if audited:
        import json as _json

        from merchant.trace import build as build_trace

        with ledger(resolved) as led:
            trace_lines = build_trace(led.store, run_id, card, paced=True)
        failed = any(l.kind == "fail" for l in trace_lines)
        runtime = (trace_lines[-1].offset / 1000) if trace_lines else 0
        payload = _json.dumps([l.as_dict() for l in trace_lines])

        # Playback is scheduled in the browser. No thread, no fake entry in
        # RUNS, and - the point of building it - no API call. The pauses are
        # the model latencies that were actually measured, so watching a replay
        # is watching the run, not an animation of it.
        trace_panel = f"""
      <div class="card flush">
        <div class="card-head">
          <div><h2>What the agent did</h2>
            <p class="sub" style="margin:2px 0 0" id="rp-sub">Everything it
              thought and did, from the audit trail.</p></div>
          <span class="sp"></span>
          <select id="rp-speed" style="width:auto;padding:4px 8px;font-size:12px">
            <option value="1">real time</option>
            <option value="4" selected>4&times;</option>
            <option value="10">10&times;</option>
          </select>
          <button class="ghost small" id="rp-go">Replay</button>
        </div>
        <div style="padding:13px 16px" id="rp-mount">
          {views.terminal(trace_lines,
                          status="failed" if failed else "complete")}
        </div>
      </div>
      <script>
      (function () {{
        var LINES = {payload};
        var RUNTIME = {runtime:.1f};
        var PROMPT = {{ok:'\u2713', tool:'\u21b3', note:'!', fail:'\u2717'}};
        var timers = [];

        function row(l) {{
          var el = document.createElement('div');
          el.className = 'at-ln ' + l.kind;
          el.innerHTML = '<span class="at-t"></span><span class="at-p"></span>' +
                         '<span class="at-x"></span>';
          el.children[0].textContent = l.at || '';
          el.children[1].textContent = PROMPT[l.kind] || '>';
          el.children[2].textContent = l.text;
          return el;
        }}

        document.getElementById('rp-go').onclick = function () {{
          timers.forEach(clearTimeout);
          timers = [];
          var speed = parseFloat(document.getElementById('rp-speed').value) || 1;
          var body = document.querySelector('#rp-mount .at-body');
          var status = document.querySelector('#rp-mount .at-status');
          body.innerHTML = '';
          status.textContent = 'REPLAYING';
          status.className = 'at-status running';
          document.getElementById('rp-sub').textContent =
            'Replaying at ' + speed + 'x. The pauses are the model latencies '
            + 'that were actually measured. No API calls.';

          var cursor = document.createElement('div');
          cursor.className = 'at-ln';
          cursor.innerHTML = '<span class="at-t"></span><span class="at-p">&gt;</span>'
            + '<span class="at-x"><span class="at-cursor"></span></span>';
          body.appendChild(cursor);

          LINES.forEach(function (l) {{
            timers.push(setTimeout(function () {{
              body.insertBefore(row(l), cursor);
              body.scrollTop = body.scrollHeight;
            }}, l.offset / speed));
          }});
          timers.push(setTimeout(function () {{
            cursor.remove();
            status.textContent = '{"FAILED" if failed else "COMPLETE"}';
            status.className = 'at-status {"failed" if failed else "complete"}';
            document.getElementById('rp-sub').textContent =
              'Everything it thought and did, from the audit trail.';
          }}, (RUNTIME * 1000) / speed + 400));
        }};
      }})();
      </script>"""

    results = ""
    if audited:
        if cards:
            section = (
                '<div style="margin:22px 0 11px;display:flex;'
                'align-items:baseline;gap:9px"><h2 style="margin:0">'
                'Needs review</h2><span class="sub">'
                f'{len(cards)} of {totals["n"]} payments</span></div>'
                + "".join(cards))
        else:
            section = ui.blank_slate(
                "Nothing was charged wrongly",
                f"All {totals['n']} deductions match your rate card and the "
                f"regulation behind it.")

        results = f"""
      {f'<div class="banner warn">{views.esc(note)}</div>' if note else ''}
      <div class="banner brand" style="margin-bottom:16px">
        <span><b>Nothing here has been disputed or written off.</b>
        Every line is a proposal waiting for you.</span>
      </div>

      <div class="card" style="padding:0;overflow:hidden;margin-bottom:16px">
        <div class="stats">
          <div class="stat"><b style="color:var(--danger)">
            {rupees(totals['recoverable_paise'])}</b>
            <span>you can ask for back</span></div>
          <div class="stat"><b>{totals['n'] - quiet}/{totals['n']}</b>
            <span>payments needing review</span></div>
          <div class="stat"><b>{totals['by_calculator']}</b>
            <span>settled by your rate card alone</span></div>
          <div class="stat"><b>{totals['queued']}</b>
            <span>waiting on your decision</span></div>
        </div>
      </div>
      {section}"""

    body = f"""
<h1>Settlement {views.esc(run_id)}</h1>
<p class="sub">{run["n_records"]} payment(s) &middot;
   {views.when(run["created_at"])}</p>

<div class="card">
  <h2>What happened to the money</h2>
  <div class="money">
    {''.join(f'<div class="lbl">{views.esc(l)}</div>'
             f'<div class="val">{rupees(a)}</div>' for l, a in money)}
    <div class="lbl total">Credited to the bank</div>
    <div class="val total">{rupees(credited)}</div>
  </div>
  <p class="sub" style="margin:12px 0 0">{
    "Every line reconciles to the paise. That is the point: the arithmetic can "
    "be perfect and the rates still wrong."
    if ties else "WARNING: these lines do not reconcile."}</p>
</div>

{control}
{trace_panel}
{results}

<div class="card tint" style="margin-top:20px">
  <h2>How this was worked out</h2>
  <p class="sub" style="margin:4px 0 0">Every figure came from your own rate
     card and the regulation behind it, compared by arithmetic. This settlement
     has no answer key &mdash; it is real data you entered &mdash; so accuracy
     is measured separately, on generated batches where the planted errors are
     known.</p>
</div>"""
    return views.page(f"Settlement {run_id}", body, "settlements", **shell)


# --- the platform, not a business ----------------------------------------

@app.get("/admin", response_class=HTMLResponse)
def admin_page(op: User = Depends(operator),
               ws: Optional[Workspace] = Depends(workspace),
               error: str = "", ok: str = ""):
    """
    Operator only. Everything here is about the platform rather than about any
    one merchant's money.

    Note what is NOT here: a way to open a customer's books. Running the
    platform is not the same as being entitled to read a contract you are not
    party to, and an operator who wants that has to be added as a member like
    anyone else - visibly, in that business's People list.
    """
    with ledger(ws.business_id if ws else None) as led:
        shell = _shell_for(led, ws)
        auth = Auth(led.conn)
        people = auth.users()
        all_businesses = led.businesses.all()
        totals = led.conn.execute(
            "SELECT COUNT(*) runs, COALESCE(SUM(n_records),0) records"
            " FROM runs").fetchone()
        findings = led.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(money_at_stake),0) stake"
            " FROM variances WHERE exception_code != 'CLEAN'").fetchone()
        denials = AccessLog(led.conn).denials()

    def role_cell(u) -> str:
        badge = ('<span class="pill brand">operator</span>' if u["is_operator"]
                 else '<span class="pill">user</span>')
        if u["user_id"] == op.user_id:
            return f'{badge} <span class="pill">you</span>'
        make = "user" if u["is_operator"] else "operator"
        label = "demote" if u["is_operator"] else "make operator"
        return (f'<span style="white-space:nowrap">{badge}'
                f' <form method="post" action="/admin/role"'
                f' style="display:inline;margin-left:6px">'
                f'<input type="hidden" name="user_id"'
                f' value="{views.esc(u["user_id"])}">'
                f'<input type="hidden" name="make" value="{make}">'
                f'<button class="ghost small">{label}</button></form></span>')

    user_rows = "".join(f"""
      <tr>
        <td>{views.esc(u["name"])}</td>
        <td class="mono">{views.esc(u["email"])}</td>
        <td>{role_cell(u)}</td>
        <td class="r">{u["businesses"]}</td>
        <td class="r">{views.when(u["created_at"])}</td>
      </tr>""" for u in people)

    banner = ""
    if error:
        banner = f'<div class="banner warn"><span>{views.esc(error)}</span></div>'
    elif ok:
        banner = f'<div class="banner brand"><span>{views.esc(ok)}</span></div>'


    biz_rows = "".join(f"""
      <tr>
        <td>{views.esc(b["name"])}</td>
        <td class="mono">{views.esc(b["business_id"])}</td>
        <td class="r">{b["payments"]}</td>
        <td class="r">{views.when(b["created_at"])}</td>
      </tr>""" for b in all_businesses)

    denial_rows = "".join(f"""
      <tr>
        <td class="r" style="color:var(--muted)">{views.when(d["at"])}</td>
        <td>{views.esc(d["email"] or "anonymous")}</td>
        <td>{views.esc(ACTION_LABEL.get(Action(d["action"]), d["action"]))}</td>
        <td class="mono">{views.esc(d["business_id"] or "")}</td>
        <td style="color:var(--muted)">{views.esc(d["detail"] or "")}</td>
      </tr>""" for d in denials)

    agent_rows = "".join(f"""
      <tr>
        <td>{views.esc(a.name)}</td>
        <td>{'<span class="pill good">live</span>' if a.is_live
             else '<span class="pill">planned</span>'}</td>
        <td style="color:var(--muted)">{views.esc(a.tagline)}</td>
      </tr>""" for a in catalog.all_agents())

    body = f"""
<h1>Platform</h1>
<p class="sub">Signed in as an operator. This is about Ledgerline, not about
   any one merchant&rsquo;s money.</p>
{banner}

<div class="card" style="padding:0;overflow:hidden">
  <div class="stats">
    <div class="stat"><b>{len(all_businesses)}</b><span>businesses</span></div>
    <div class="stat"><b>{len(people)}</b><span>accounts</span></div>
    <div class="stat"><b>{totals["runs"]}</b><span>settlements audited</span></div>
    <div class="stat"><b>{findings["n"]}</b><span>findings raised</span></div>
    <div class="stat"><b>{rupees(findings["stake"])}</b>
      <span>surfaced across the platform</span></div>
  </div>
</div>

<div class="card flush">
  <div class="card-head"><h2>Agent rollout</h2></div>
  <table><tr><th>Agent</th><th>Status</th><th>What it does</th></tr>
    {agent_rows}</table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    A planned agent has no implementation and cannot be switched on for anyone.
    Adding the next one means writing a runner and flipping a status.
  </div>
</div>

<div class="card flush">
  <div class="card-head"><h2>Accounts</h2></div>
  <table><tr><th>Name</th><th>Email</th><th>Role</th>
    <th class="r">Businesses</th><th class="r">Joined</th></tr>
    {user_rows}</table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    An operator can reach this page and nothing else that an ordinary account
    cannot. Promoting somebody does not give them sight of any
    business&rsquo;s money. Every change here is recorded.
  </div>
</div>

<div class="card flush">
  <div class="card-head"><h2>Businesses</h2></div>
  <table><tr><th>Name</th><th>ID</th><th class="r">Payments</th>
    <th class="r">Created</th></tr>
    {biz_rows}</table>
</div>

<div class="card flush">
  <div class="card-head"><h2>Refused access attempts</h2></div>
  <table>
    <tr><th class="r">When</th><th>Who</th><th>What</th><th>Business</th>
        <th>Detail</th></tr>
    {denial_rows or '<tr><td colspan="5" class="empty">'
     'Nothing has been refused.</td></tr>'}
  </table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    Refusals only. An operator investigating an incident needs to know someone
    tried to reach a business they are not in. They do not need, and are not
    entitled to, what that business&rsquo;s settlements say.
  </div>
</div>

<div class="card tint">
  <h2>What an operator cannot do here</h2>
  <p class="sub" style="margin:4px 0 0">Open a customer&rsquo;s books. Running
     the platform is not the same as being entitled to read a contract you are
     not party to. To see a business&rsquo;s findings, an operator has to be
     added as a member like anyone else &mdash; visibly, in that
     business&rsquo;s People list, where its owner can see it and remove it.</p>
</div>"""
    return views.page("Platform", body, "admin", **shell)


@app.post("/admin/role")
def change_role(request: Request, user_id: str = Form(...),
                make: str = Form(...), op: User = Depends(operator)):
    """
    Grant or withdraw the operator flag.

    An operator cannot demote themselves. That single refusal is what keeps the
    platform reachable: every other demotion is performed by somebody who stays
    an operator afterwards, so the count can never fall to zero. Without it, the
    last operator could switch their own flag off and leave nobody able to turn
    it back on - recoverable only by editing the database by hand.
    """
    from urllib.parse import quote

    from merchant.ratelimit import client_address

    address = client_address(request)
    wants_operator = make == "operator"

    with ledger() as led:
        auth = Auth(led.conn)
        log = AccessLog(led.conn)
        target = auth.by_id(user_id)

        def refuse(why: str):
            log.denied(Action.CHANGE_ROLE, user=op, target=user_id,
                       address=address, detail=why)
            return RedirectResponse(f"/admin?error={quote(why)}", status_code=303)

        if target is None:
            return refuse("No such account.")
        if target.user_id == op.user_id:
            return refuse("You cannot change your own role. Somebody has to be "
                          "able to reach this page tomorrow.")
        if target.is_operator == wants_operator:
            return refuse(f"{target.email} is already "
                          f"{'an operator' if wants_operator else 'an ordinary user'}.")

        auth.set_operator(user_id, wants_operator)
        log.record(Action.CHANGE_ROLE, user=op, target=user_id, address=address,
                   detail=f"{target.email} "
                          f"{'promoted to operator' if wants_operator else 'returned to ordinary user'}")

    done = "is now an operator" if wants_operator else "is now an ordinary user"
    return RedirectResponse(f"/admin?ok={quote(f'{target.email} {done}.')}",
                            status_code=303)


# --- who works in this business ------------------------------------------

@app.get("/people", response_class=HTMLResponse)
def people_page(ws: Workspace = Depends(required_workspace), error: str = "",
                ok: str = ""):
    ws.require_owner("who works in this business")

    from merchant.auth import ROLE_BLURB, ROLE_LABEL

    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        members = Auth(led.conn).members_of(ws.business_id)

    rows = "".join(f"""
      <tr>
        <td>{views.esc(m["name"])}{' <span class="pill">you</span>'
             if m["user_id"] == ws.user.user_id else ''}</td>
        <td class="mono">{views.esc(m["email"])}</td>
        <td><span class="pill {'brand' if m["role"] == 'owner' else ''}">
          {views.esc(ROLE_LABEL[Role(m["role"])])}</span></td>
        <td class="r">{views.when(m["added_at"])}</td>
        <td class="r">{'' if m["user_id"] == ws.user.user_id else
          f'<form method="post" action="/people/remove" style="display:inline">'
          f'<input type="hidden" name="user_id" value="{views.esc(m["user_id"])}">'
          f'<button class="ghost small">remove</button></form>'}</td>
      </tr>""" for m in members)

    banner = ""
    if error:
        banner = f'<div class="banner warn"><span>{views.esc(error)}</span></div>'
    elif ok:
        banner = f'<div class="banner brand"><span>{views.esc(ok)}</span></div>'

    body = f"""
{banner}
<h1>People</h1>
<p class="sub">Who can see this business&rsquo;s findings, and who can change
   what they are measured against.</p>

<div class="card flush">
  <div class="card-head"><h2>Members</h2></div>
  <table><tr><th>Name</th><th>Email</th><th>Role</th><th class="r">Added</th>
    <th class="r"></th></tr>{rows}</table>
</div>

<div class="card">
  <h2>Add someone</h2>
  <p class="sub" style="margin:3px 0 12px">They need an account already. There
     are no email invitations yet.</p>
  <form method="post" action="/people/add">
    <div class="row">
      <div><label>Their email</label>
        <input name="email" type="email" required></div>
      <div style="flex:0 0 150px"><label>Role</label>
        <select name="role">
          <option value="staff">Staff</option>
          <option value="owner">Owner</option>
        </select></div>
      <div style="flex:0"><button>Add</button></div>
    </div>
  </form>
  <div style="margin-top:14px">
    {''.join(f'<p class="sub" style="margin:0 0 5px;font-size:11.8px">'
             f'<b>{views.esc(ROLE_LABEL[r])}</b> &mdash; {views.esc(ROLE_BLURB[r])}</p>'
             for r in Role)}
  </div>
</div>"""
    return views.page("People", body, "people", **shell)


@app.post("/people/add")
def add_person(email: str = Form(...), role: str = Form("staff"),
               ws: Workspace = Depends(required_workspace)):
    ws.require_owner("who works in this business")

    from urllib.parse import quote

    with ledger(ws.business_id) as led:
        auth = Auth(led.conn)
        row = auth.by_email(email)
        if row is None:
            return RedirectResponse(
                f"/people?error={quote('No account with that email. They need to sign up first.')}",
                status_code=303)
        auth.add_member(ws.business_id, row["user_id"], Role(role))
    return RedirectResponse(f"/people?ok={quote(f'{email} added as {role}.')}",
                            status_code=303)


@app.post("/people/remove")
def remove_person(user_id: str = Form(...),
                  ws: Workspace = Depends(required_workspace)):
    ws.require_owner("who works in this business")

    from urllib.parse import quote

    with ledger(ws.business_id) as led:
        auth = Auth(led.conn)
        role = auth.conn.execute(
            "SELECT role FROM memberships WHERE business_id = ? AND user_id = ?",
            (ws.business_id, user_id)).fetchone()
        # A business with no owner is one nobody can ever correct the rate card
        # of - and the rate card is what every finding is measured against.
        if role and role["role"] == str(Role.OWNER) \
                and auth.owner_count(ws.business_id) <= 1:
            return RedirectResponse(
                f"/people?error={quote('That is the only owner. A business without one is a business nobody can correct.')}",
                status_code=303)
        auth.remove_member(ws.business_id, user_id)
    return RedirectResponse("/people?ok=Removed.", status_code=303)


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request,
                  ws: Workspace = Depends(required_workspace)):
    """
    Who saw this business's money data, and who was refused.

    Owner-only, and reading it is itself recorded - a blind spot at the most
    sensitive page is where someone would look first.
    """
    ws.require_owner("who can see this activity log", request)

    from merchant.ratelimit import client_address

    with ledger(ws.business_id) as led:
        shell = _shell_for(led, ws)
        log = AccessLog(led.conn)
        log.record(Action.VIEW_ACCESS_LOG, user=ws.user,
                   business_id=ws.business_id, address=client_address(request))
        entries = log.for_business(ws.business_id)
        counts = log.counts(ws.business_id)

    rows = "".join(f"""
      <tr>
        <td class="r" style="color:var(--muted)">{views.when(e.at)}</td>
        <td>{views.esc(e.email)}</td>
        <td>{views.esc(ACTION_LABEL.get(Action(e.action), e.action))}</td>
        <td class="mono">{views.esc(e.target or "")}</td>
        <td>{'<span class="pill danger">refused</span>' if e.denied
             else '<span class="pill">allowed</span>'}</td>
        <td style="color:var(--muted)">{views.esc(e.detail or "")}</td>
      </tr>""" for e in entries)

    body = f"""
<h1>Activity</h1>
<p class="sub">Every time someone opened a settlement, read a dispute letter,
   asked the agent about these books, or was refused.</p>

<div class="card" style="padding:0;overflow:hidden">
  <div class="stats">
    <div class="stat"><b>{counts["total"]}</b><span>recorded events</span></div>
    <div class="stat {'bad' if counts["denied"] else ''}">
      <b>{counts["denied"]}</b><span>refused</span></div>
    <div class="stat"><b>{counts["people"]}</b><span>people</span></div>
  </div>
</div>

<div class="card flush">
  <table>
    <tr><th class="r">When</th><th>Who</th><th>What</th><th>Target</th>
        <th>Outcome</th><th>Detail</th></tr>
    {rows or '<tr><td colspan="6" class="empty">Nothing recorded yet.</td></tr>'}
  </table>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    Append-only. Nothing in this application updates or deletes a row here, and
    a test checks that it stays true &mdash; a log that can be edited is not an
    audit log, it is a list.
  </div>
</div>"""
    return views.page("Activity", body, "activity", **shell)


@app.get("/about", response_class=HTMLResponse)
def about_page(user: Optional[User] = Depends(maybe_user),
               ws: Optional[Workspace] = Depends(workspace)):
    with ledger(ws.business_id if ws else None) as led:
        shell = _shell_for(led, ws)

    body = """
<h1>What is real here, and what is not</h1>
<p class="sub">Worth being precise about, because the difference is the whole
   argument.</p>

<div class="card">
  <h2>What this platform is</h2>
  <p class="sub">Not a place to record sales. Your money already flows through
     your gateway; this reads what the gateway did to it afterwards and says
     which parts were wrong. A settlement report proves the subtraction was
     right. Nothing proves the <i>rate</i> was right. That is the gap.</p>
</div>

<div class="card">
  <h2>Simulated: where the data comes from</h2>
  <p class="sub">In production, settlements arrive over the Razorpay API and
     nobody types anything. To demonstrate an auditor you need settlement
     reports that contain real errors, and those do not exist to borrow &mdash;
     no merchant publishes labelled overcharges, and test mode does not settle
     at all. So there is a simulator: a fake merchant and a fake gateway whose
     only job is to manufacture settlements to audit.</p>
  <p class="sub">Its misbehaviour is a visible switch rather than a hidden
     fixture, and it imports nothing from the auditor &mdash; it genuinely does
     not know what the correct answer is. Everything downstream is identical
     whether a settlement came from Razorpay or from the simulator. That is the
     point of separating them.</p>
</div>

<div class="card">
  <h2>Real</h2>
  <p class="sub">Everything after the settlement. Each business&rsquo;s rate card
     carries the RBI circular or statute behind every regulated rate. The
     expected-value engine is plain Python, unit-tested against those sources,
     and computes every rupee figure on this site &mdash; the agent never does
     arithmetic. Razorpay&rsquo;s field names and id formats were verified
     against their live test API.</p>
</div>

<div class="card">
  <h2>Measured</h2>
  <p class="sub">Accuracy is not claimed from this site. It is measured on
     generated batches where the planted errors are known: 300 records across
     five independent batches, 70 planted anomalies all found, zero false
     accusations. A settlement you enter here has no answer key, so no accuracy
     figure is shown next to it.</p>
</div>

<div class="card">
  <h2>Not built: accounts</h2>
  <p class="sub">There is no login and no password. Anyone who can reach this
     page can open any business on it. That is a deliberate omission for a
     prototype rather than an oversight &mdash; but it means this is not
     something to point at real merchant data.</p>
</div>

<div class="card">
  <h2>The guardrails</h2>
  <p class="sub">The agent has five tools and all five are read-only &mdash; it
     cannot write to a ledger because no such tool exists in its world. Every
     figure it states is checked against what the engine computed. Low
     confidence, real money, or a correction applied during review sends a
     finding to a human queue. Nothing here is ever marked reviewed by the
     system.</p>
</div>"""
    return views.page("About", body, "about", **shell)
