"""
Turning an API failure into something a merchant can act on.

## Why this exists

Every agent in this platform falls back to arithmetic when a model call fails,
which is the right behaviour and was already true. What was not true is that
the FAILURE was legible. A supplier row read:

    The agent could not be reached (BadRequestError: Error code: 400 -
    {'type': 'error', 'error': {'type': 'invalid_request_error', 'message':
    'Your credit balance is too low...

That is a Python traceback in a finance product. A person seeing it concludes
the tool is broken, when in fact every score, pattern and recommendation on the
page beside it is correct and was computed without the model. The failure is
recoverable and mundane - somebody needs to top up an account - and it should
read that way.

## Three states worth telling apart

    NOT CONFIGURED   no key at all. Extremely common: anybody who clones the
                     repo and runs it. This used to raise a TypeError out of
                     the SDK and take the whole run down with it, which turned
                     "you have not set an env var" into "the demo crashes".

    NO CREDITS       key is fine, account is empty. The wall this project hit
                     twice. Distinguishing it from a bad key matters, because
                     the fixes are completely different and one of them is
                     "rotate your key", which does nothing here.

    TRANSIENT        rate limited, overloaded, a network blip. Worth retrying;
                     the other two are not.

## Fatal means "stop asking", not "give up"

A run over fifty suppliers with an empty account makes fifty round trips that
each fail identically. They cost nothing in tokens, but they cost the merchant
a minute of watching a progress bar fill up with failures. So the first fatal
error stops the remaining calls, and the rows that were never attempted say so
rather than claiming the agent was asked and declined.
"""

from __future__ import annotations

from typing import Optional

# Errors where retrying is pointless because every remaining call fails the
# same way. Matched on the message rather than the exception class: the SDK
# wraps several of these in the same type, and the message is what actually
# distinguishes them.
FATAL_MARKERS = ("credit balance", "authentication", "invalid x-api-key",
                 "permission", "could not resolve authentication")

NOT_CONFIGURED = "not_configured"
NO_CREDITS = "no_credits"
BAD_KEY = "bad_key"
NO_ACCESS = "no_access"
TRANSIENT = "transient"
UNKNOWN = "unknown"

# What to say, and what the person reading it should do about it. Second
# sentence is always the reassurance, because it is always true and it is the
# thing a person needs to hear before they decide the product is broken.
EXPLANATION = {
    NOT_CONFIGURED:
        "No ANTHROPIC_API_KEY is set, so the agent was not asked. Every score, "
        "pattern and recommendation here was computed without it - what is "
        "missing is the written explanation, not the analysis.",
    NO_CREDITS:
        "The Anthropic account has no credits left, so the agent could not be "
        "asked. Every score, pattern and recommendation here was computed "
        "without it - what is missing is the written explanation, not the "
        "analysis.",
    BAD_KEY:
        "The ANTHROPIC_API_KEY was rejected. Every score, pattern and "
        "recommendation here was computed without the agent - what is missing "
        "is the written explanation, not the analysis.",
    NO_ACCESS:
        "This API key is not permitted to use the model this agent runs on. "
        "Every score, pattern and recommendation here was computed without it.",
    TRANSIENT:
        "The API did not answer in time. Every score, pattern and "
        "recommendation here was computed without the agent; running it again "
        "will usually pick up the explanations.",
    UNKNOWN:
        "The agent could not be reached. Every score, pattern and "
        "recommendation here was computed without it - what is missing is the "
        "written explanation, not the analysis.",
}

# The one-line version, for a table cell or a row where the paragraph above
# would not fit.
SHORT = {
    NOT_CONFIGURED: "no API key set",
    NO_CREDITS: "the Anthropic account is out of credits",
    BAD_KEY: "the API key was rejected",
    NO_ACCESS: "this key cannot use that model",
    TRANSIENT: "the API did not answer",
    UNKNOWN: "the agent could not be reached",
}


def kind(error: Optional[str]) -> str:
    """Which of the failures this is, from the message."""
    text = (error or "").lower()
    if not text:
        return UNKNOWN
    # Checked before the generic authentication marker: the SDK's message for
    # a missing key mentions authentication too, and the fixes differ.
    if "could not resolve authentication" in text or \
            "expected one of api_key" in text:
        return NOT_CONFIGURED
    if "credit balance" in text:
        return NO_CREDITS
    if "invalid x-api-key" in text or "authentication" in text:
        return BAD_KEY
    if "permission" in text or "not_found_error" in text:
        return NO_ACCESS
    if "rate limit" in text or "overloaded" in text or "timeout" in text \
            or "timed out" in text:
        return TRANSIENT
    return UNKNOWN


def explain(error: Optional[str]) -> str:
    """A sentence a merchant can act on, instead of an exception."""
    return EXPLANATION[kind(error)]


def short(error: Optional[str]) -> str:
    return SHORT[kind(error)]


def is_fatal(error: Optional[str]) -> bool:
    """Whether asking again, for this or any other record, is pointless."""
    return bool(error) and any(m in error.lower() for m in FATAL_MARKERS)
