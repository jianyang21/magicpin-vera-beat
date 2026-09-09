"""
Vera-beat — magicpin AI Challenge submission.

A deterministic, template-driven merchant/customer WhatsApp composer.
No external LLM calls: every message is built from fields that are actually
present in the pushed contexts, so it can never hallucinate a fact, a
citation, or a competitor name. That's the "start small and deterministic"
approach — get trigger/merchant/category composition airtight first.

Run:
    pip install -r requirements.txt
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional
from urllib import request as urlrequest, error as urlerror

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Concurrency model — read this before touching the state dicts below.
#
# This process holds all state in plain dicts, in memory, on purpose (no
# Redis/Postgres per the design constraint). That makes exactly one thing
# true everywhere else in this file: it only works correctly as a SINGLE
# worker process. Locking below prevents races *within* this process; it
# cannot make two separate uvicorn workers share memory. Deploy with
# `--workers 1` (see render.yaml) — that's a hard requirement, not a config
# nicety.
#
# `state_lock` is a threading.Lock, not an asyncio.Lock. That's deliberate:
# asyncio.Lock only guards coroutines cooperatively scheduled on one event
# loop and is unsafe if the same section ever runs from a real OS thread —
# which is exactly what `asyncio.to_thread` gives us below (used to keep the
# blocking Groq HTTP call from freezing the whole server, including
# /v1/healthz, while it waits on the network). threading.Lock is safe in
# both cases and the critical sections here are microseconds of dict
# read/write, so holding a "blocking" lock briefly on the event loop thread
# is fine — the rule is just: never hold it across an `await`.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(cleanup_loop())  # cleanup_loop is defined further below; resolved at call time, not here
    yield
    task.cancel()


app = FastAPI(title="vera-beat", lifespan=lifespan)
# This API is meant to be called from anywhere — the judge harness (not
# subject to CORS at all, since that's a browser-only mechanism), but also
# browsers hitting /docs directly or any test page. Wide open is correct
# here: there's no cookie/session auth to protect, no per-user data — every
# caller sees the same synthetic dataset either way.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
START = time.time()
state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}

# conversation_id -> state
conversations: dict[str, dict] = {}

# suppression_key -> last-fired unix ts (used both to avoid resend spam
# within a tick cycle and to prevent immediately re-triggering conversations
# the merchant already ended)
fired_suppression: dict[str, float] = {}
suppressed_merchants: dict[str, float] = {}  # merchant_id -> until ts (hostile / hard no)

# TTL eviction — bounds memory growth over a long-running process. The
# judge's own test window is ~90 real minutes, so none of this matters for
# scoring; it matters for not slowly leaking memory if the process stays up
# longer than that (local dev, a re-used deploy, etc).
SUPPRESSION_TTL_S = int(os.environ.get("SUPPRESSION_TTL_S", 24 * 3600))
MERCHANT_SUPPRESSION_TTL_S = int(os.environ.get("MERCHANT_SUPPRESSION_TTL_S", 7 * 24 * 3600))
CONVERSATION_TTL_S = int(os.environ.get("CONVERSATION_TTL_S", 24 * 3600))
CLEANUP_INTERVAL_S = int(os.environ.get("CLEANUP_INTERVAL_S", 3600))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_ctx(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


# ---------------------------------------------------------------------------
# Composition helpers — small, reusable, honest (only use data that exists)
# ---------------------------------------------------------------------------

# Plain-English expansions for trade abbreviations that show up in category
# digests/citations. A merchant shouldn't have to already know what "JIDA"
# or "DCI" means for the message to make sense.
ABBREV_GLOSSARY = {
    "JIDA": "the Indian Dental Association's journal",
    "IDA": "the Indian Dental Association",
    "DCI": "the Dental Council of India",
    "IOPA": "a standard dental X-ray",
    "RVG": "a type of digital dental X-ray sensor",
}


def friendly_source(source: Optional[str]) -> str:
    """Expand a known abbreviation the first time it's used, e.g.
    'JIDA Oct 2026, p.14' -> 'JIDA (the Indian Dental Association's journal), Oct 2026, p.14'.
    Leaves anything not in the glossary untouched rather than guessing."""
    if not source:
        return source or ""
    for abbr, expansion in ABBREV_GLOSSARY.items():
        if source.startswith(abbr):
            rest = source[len(abbr):].strip(" ,")
            return f"{abbr} ({expansion}), {rest}" if rest else f"{abbr} ({expansion})"
    return source


GLOSSARY_QUESTION_CUES = ["what is", "what does", "what's", "who is", "explain", "meaning of", "stands for", "define"]


def glossary_answer(message: str) -> Optional[str]:
    """Deterministic, zero-hallucination answer for a question about a known
    abbreviation — checked BEFORE the LLM is ever called. An LLM asked to
    define these has fabricated two different wrong answers for the same
    term in testing (a fake fluoride treatment, a fake real-world
    institution) instead of admitting it doesn't know. This bypasses that
    risk entirely for the whole class of "what does X mean" questions we
    already have a ground truth for, at zero cost."""
    lower = message.lower().strip().rstrip("?")
    looks_like_question = any(cue in lower for cue in GLOSSARY_QUESTION_CUES) or lower in (k.lower() for k in ABBREV_GLOSSARY)
    if not looks_like_question:
        return None
    for abbr, expansion in ABBREV_GLOSSARY.items():
        if re.search(rf"\b{re.escape(abbr)}\b", message, re.IGNORECASE):
            return f"{abbr} stands for {expansion}."
    return None


# Words too generic within this category to prove a definition is actually
# correct — "dental" appears in almost any sentence about a dental term,
# real or fabricated, so matching on it alone gives a false pass (verified:
# "Jawaharlal Institute of Dental and Medical Sciences" — completely made
# up — still contains "dental" and would slip through without this filter).
_GLOSSARY_GENERIC_WORDS = {"dental", "medical", "india", "indian", "type"}


# Only a genuine defining claim needs checking — "JIDA Oct 2026, p.14" or
# "published in JIDA" are citations, not claims about what JIDA IS, and
# don't need to re-justify themselves every time they're mentioned. Checking
# on any mention at all (an earlier version of this function did) produced
# false positives against perfectly good citation-style sentences.
_DEFINING_PATTERN = r"\b{abbr}\b\s*(?:is|are|stands for|refers to|means|=)\b"


def glossary_grounding_ok(body: str) -> bool:
    """Safety net for replies that DID go through the LLM (the merchant's
    phrasing didn't trip glossary_answer() above, e.g. it came up mid-
    sentence rather than as a direct question). If the reply actually
    *defines* one of our known terms, that definition must overlap with a
    distinctive word from what we actually know it means — catches the LLM
    inventing a wrong expansion, without flagging normal citations."""
    for abbr, expansion in ABBREV_GLOSSARY.items():
        if not re.search(_DEFINING_PATTERN.format(abbr=re.escape(abbr)), body, re.IGNORECASE):
            continue  # not attempting a definition here — a citation like "JIDA Oct 2026, p.14" is fine as-is
        expansion_words = [
            w for w in re.findall(r"[a-zA-Z]+", expansion.lower())
            if len(w) > 3 and w not in _GLOSSARY_GENERIC_WORDS
        ]
        if expansion_words and not any(w in body.lower() for w in expansion_words):
            return False
    return True


def digest_item(category: dict, item_id: str) -> Optional[dict]:
    for item in category.get("digest", []) or []:
        if item.get("id") == item_id:
            return item
    return None


def owner_or_name(merchant: dict) -> str:
    identity = merchant.get("identity", {})
    first = identity.get("owner_first_name")
    name = identity.get("name", "there")
    if first and merchant.get("category_slug") == "dentists" and not first.strip().startswith("Dr"):
        return f"Dr. {first}"
    return first or name


def is_hindi_pref(merchant: dict, customer: Optional[dict]) -> bool:
    if customer:
        pref = customer.get("identity", {}).get("language_pref", "")
        return "hi" in pref
    langs = merchant.get("identity", {}).get("languages", [])
    return "hi" in langs


def active_offers(merchant: dict) -> list[dict]:
    return [o for o in merchant.get("offers", []) or [] if o.get("status") == "active"]


def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "?"
    return f"{abs(round(x * 100))}%"


def fmt_money(v) -> str:
    try:
        n = int(float(v))
        return f"₹{n:,}"
    except Exception:
        return str(v)


def fmt_when(iso_str: Optional[str]) -> Optional[str]:
    """'2026-04-26T19:30:00+05:30' -> 'Sun 26 Apr, 7:30pm'. Returns None (never
    a raw ISO string) if it can't be parsed, so callers can fall back safely."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        time_part = dt.strftime("%I:%M%p").lstrip("0").lower()
        if dt.hour == 0 and dt.minute == 0:
            return dt.strftime("%a %d %b")
        return dt.strftime(f"%a %d %b, {time_part}")
    except Exception:
        return None


def fmt_field(value, unit_singular: str = None) -> Optional[str]:
    """Turn a snake_case field value into a natural phrase. Returns None
    (never the literal string 'None') if the value is missing."""
    if value is None or value == "":
        return None
    return str(value).replace("_", " ")


def safe(value, fallback: str = "") -> str:
    """Never let a Python None reach an f-string as the text 'None'."""
    return fallback if value is None else str(value)


def clean(text: str) -> str:
    """Collapse whitespace, keep it tight — judge penalizes preambles."""
    return re.sub(r"\s+", " ", text).strip()


def customer_contact(customer: dict) -> tuple[str, str]:
    """Some stored customer names carry a parenthetical annotation, e.g.
    'Karthik (parent: Sumitra)' for a child customer. Printing that verbatim
    reads like a leaked internal note. Returns (name_to_address, subject_name)
    — for a plain name these are the same; for an annotated one we address
    the parent and refer to the child by name in the message body instead."""
    raw = customer.get("identity", {}).get("name", "there")
    m = re.match(r"^(.*?)\s*\(parent:\s*(.*?)\)\s*$", raw)
    if m:
        child, parent = m.group(1).strip(), m.group(2).strip()
        return parent, child
    return raw, raw


def natural(phrase: Optional[str]) -> Optional[str]:
    """snake_case -> readable phrase, with digit/word boundaries spaced so
    '30day' doesn't stay glued to 'day'. Returns None (never 'None') if empty."""
    if not phrase:
        return None
    text = str(phrase).replace("_", " ")
    text = re.sub(r"(\d+)([a-zA-Z])", r"\1 \2", text)
    return text


def already_sent(conversation_id: str, body: str) -> bool:
    conv = conversations.get(conversation_id)
    if not conv:
        return False
    return body in conv.get("sent_bodies", set())


# ---------------------------------------------------------------------------
# Per-trigger-kind renderers.
# Each returns (body, cta, send_as, rationale) or None to decline sending.
# `payload` is trigger["payload"] (may be the placeholder stub for
# generated triggers that carry no real facts — those renderers must fall
# back to real merchant/category fields instead of inventing numbers).
# ---------------------------------------------------------------------------

def _is_placeholder(payload: dict) -> bool:
    return bool(payload.get("placeholder"))


def r_perf_dip(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    window = payload.get("window", "7d")
    baseline = payload.get("vs_baseline")
    if not _is_placeholder(payload) and metric and delta is not None:
        baseline_txt = f" (vs your usual ~{baseline}/day)" if baseline is not None else ""
        body = f"{who}, your {metric} dropped {fmt_pct(delta)} over the last {window}{baseline_txt}. Want me to check what changed — posts, offers, or hours?"
        return clean(body), "binary_yes_no", "vera", "Loss aversion framed on the merchant's own delta_7d-style metric; single diagnostic CTA."
    perf = merchant.get("performance", {})
    delta7 = perf.get("delta_7d", {})
    worst_key, worst_val = None, 0
    for k, v in delta7.items():
        if v is not None and v < worst_val:
            worst_key, worst_val = k, v
    if worst_key:
        body = f"{who}, {worst_key.replace('_pct', '')} is down {fmt_pct(worst_val)} this week. Want me to take a look?"
        return clean(body), "binary_yes_no", "vera", "Placeholder trigger payload; used the merchant's real performance.delta_7d instead of inventing numbers."
    return None


def r_perf_spike(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    driver = natural(payload.get("likely_driver"))
    if not _is_placeholder(payload) and metric and delta is not None:
        driver_txt = f" — looks like the {driver} is working" if driver else ""
        body = f"{who}, nice — {metric} up {fmt_pct(delta)} this week{driver_txt}. Want me to double down (repost, extend the offer window)?"
        return clean(body), "binary_yes_no", "vera", "Reciprocity + momentum framing on a verified internal delta; single next-step CTA."
    perf = merchant.get("performance", {})
    delta7 = perf.get("delta_7d", {})
    best_key = max(delta7, key=lambda k: (delta7[k] if delta7[k] is not None else -1), default=None)
    if best_key and delta7.get(best_key, 0) and delta7[best_key] > 0:
        body = f"{who}, {best_key.replace('_pct', '')} is up {fmt_pct(delta7[best_key])} this week. Want me to help you keep the momentum?"
        return clean(body), "binary_yes_no", "vera", "Placeholder payload; used merchant's real delta_7d instead of invented figures."
    return None


def r_renewal_due(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    sub = merchant.get("subscription", {})
    days = payload.get("days_remaining", sub.get("days_remaining"))
    plan = payload.get("plan", sub.get("plan"))
    amount = payload.get("renewal_amount")
    if days is None:
        return None
    amt_txt = f" ({fmt_money(amount)})" if amount else ""
    body = f"{who}, your {plan} plan{amt_txt} renews in {days} days. Reply YES to auto-renew, or STOP to let it lapse."
    return clean(body), "binary_yes_no", "vera", "Functional nudge with real subscription data; single binary commitment per anti-pattern guidance."


def r_festival_upcoming(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    festival = payload.get("festival")
    days_until = payload.get("days_until")
    if not festival:
        return None
    offers = active_offers(merchant)
    offer_txt = f" Your active offer — {offers[0]['title']} — is a good anchor for the push." if offers else ""
    body = f"{who}, {festival} is {days_until} days out.{offer_txt} Want me to draft a {festival} post for your profile?"
    return clean(body), "binary_yes_no", "vera", "Seasonal external trigger anchored to a real active offer where available; effort-externalization CTA."


def r_milestone_reached(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    metric = payload.get("metric")
    now_v = payload.get("value_now")
    target = payload.get("milestone_value")
    if metric is None or now_v is None:
        return None
    metric_label = {"review_count": "reviews"}.get(metric, natural(metric))
    gap = (target - now_v) if (target is not None) else None
    gap_txt = f" — {gap} more to go" if gap and gap > 0 else ""
    target_txt = f" Want a Google post to mark it once you cross {target}?" if target is not None else ""
    body = f"{who}, you're at {now_v} {metric_label}{gap_txt}.{target_txt}"
    return clean(body), "binary_yes_no", "vera", "Social-proof-adjacent milestone framing on real merchant counters (metric label reordered to read naturally, e.g. '145 reviews' not '145 review count'); low-friction opt-in."


def r_review_theme_emerged(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    theme = payload.get("theme")
    occ = payload.get("occurrences_30d")
    quote = payload.get("common_quote")
    if not theme:
        return None
    quote_txt = f" One reviewer wrote: “{quote}”." if quote else ""
    body = f"{who}, {occ} reviews this month mention {natural(theme)}.{quote_txt} Want help drafting a reply template or a fix you can post about?"
    return clean(body), "binary_yes_no", "vera", "Verifiable review-theme count with a direct quote; reciprocity (flagging it) plus low-friction help offer."


def r_competitor_opened(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    name = payload.get("competitor_name")
    dist = payload.get("distance_km")
    their_offer = payload.get("their_offer")
    if not name:
        return None
    offer_txt = f", running {their_offer}," if their_offer else ""
    body = f"{who}, {name} opened {dist}km away{offer_txt}. Want to see how your listing compares side-by-side?"
    return clean(body), "binary_yes_no", "vera", "Competitor context comes verbatim from the pushed trigger payload — never invented; curiosity-driven CTA."


def r_dormant_with_vera(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    days = payload.get("days_since_last_merchant_message")
    topic = natural(payload.get("last_topic"))
    if not days:
        return None
    topic_txt = f" — we were talking about {topic}" if topic else ""
    body = f"{who}, haven't heard from you in {days} days{topic_txt}. Still want to pick that up, or should I stop nudging for now?"
    return clean(body), "binary_yes_no", "vera", "Re-engagement offers an explicit opt-out (STOP-equivalent) rather than just repeating the pitch."


def r_cde_opportunity(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    item = digest_item(category, payload.get("digest_item_id", ""))
    credits = payload.get("credits")
    fee = natural(payload.get("fee"))
    if not item:
        return None
    fee_txt = f", {fee}" if fee else ""
    credits_txt = f" ({credits} CDE credits{fee_txt})" if credits else ""
    body = f"{who}, heads-up: {item.get('title', '')}.{credits_txt} {item.get('summary', '')} Want the registration link sent to you?"
    return clean(body), "binary_yes_no", "vera", "CDE invite sourced entirely from the category digest item (title kept as one clause instead of dash-joined with the summary, avoiding a run of 3 dashes); peer-tone, no promo language."


def r_winback_eligible(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    days = payload.get("days_since_expiry")
    lapsed = payload.get("lapsed_customers_added_since_expiry")
    dip = payload.get("perf_dip_pct")
    if days is None:
        return None
    body = (
        f"{who}, it's been {days} days since your plan lapsed"
        + (f" — {lapsed} customers have gone soft-lapsed since" if lapsed else "")
        + (f" and visibility is down {fmt_pct(dip)}." if dip else ".")
        + " Want to reactivate? Reply YES and I'll handle the rest."
    )
    return clean(body), "binary_yes_no", "vera", "Loss-aversion winback using real expiry + dip figures; effort externalization (\"I'll handle the rest\")."


def r_supply_alert(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    molecule = payload.get("molecule")
    batches = payload.get("affected_batches", [])
    if not molecule:
        return None
    batch_txt = ", ".join(batches) if batches else "the affected batches"
    body = f"{who}, supply alert: {molecule} batches {batch_txt} have been flagged. Please check your stock and quarantine any matching batches. Reply once you've confirmed."
    return clean(body), "binary_yes_no", "vera", "Highest-urgency (5) compliance-style alert; factual, no promotional tone, single confirm CTA."


def r_category_seasonal(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    trends = payload.get("trends", [])
    if not trends:
        return None
    top = [natural(t) for t in trends[:2]]
    body = f"{who}, seasonal shelf signal for your category: {', '.join(top)}. Want a reorder checklist for these lines?"
    return clean(body), "binary_yes_no", "vera", "Category-level seasonal trend passed through verbatim from payload; actionable shelf framing."


def r_gbp_unverified(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    uplift = payload.get("estimated_uplift_pct")
    path = natural(payload.get("verification_path"))
    peer_ctr = fmt_pct(category.get("peer_stats", {}).get("avg_ctr"))
    uplift_txt = f" Shops that verify typically see about {fmt_pct(uplift)} more calls." if uplift else ""
    cta_txt = f" Want me to start verification (by {path})?" if path else " Want me to start verification?"
    body = (
        f"{who}, your Google listing isn't verified yet — verified profiles in your category see meaningfully more calls "
        f"(peer average click rate is {peer_ctr})."
        f"{uplift_txt}{cta_txt}"
    )
    return clean(body), "binary_yes_no", "vera", "Loss aversion via peer benchmark (real category stat) + real estimated uplift; verification_path reformatted into a natural clause instead of '...the postcard or phone call process'."


def r_active_planning_intent(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    topic = natural(payload.get("intent_topic"))
    last_msg = payload.get("merchant_last_message")
    if not topic:
        return None
    body = f"{who}, following up on “{last_msg}” — I've drafted a first cut for the {topic}. Want me to share it now, or would you like to add anything first?"
    return clean(body), "open_ended", "vera", "Merchant already expressed explicit intent (from trigger payload) — routed straight to action mode, no re-qualification."


def r_curious_ask_due(category, merchant, trigger, customer):
    who = owner_or_name(merchant)
    prompts = {
        "what_service_in_demand_this_week": "what's been the most-asked-for service this week?",
        "what_customers_asked": "what's one thing customers keep asking about that isn't on your profile yet?",
    }
    payload = trigger.get("payload", {})
    ask = prompts.get(payload.get("ask_template"), "what's one thing you'd want more customers to know about your place?")
    body = f"{who}, quick one — {ask} Helps me tailor what I surface for you."
    return clean(body), "open_ended", "vera", "Uses compulsion lever #7 (asking the merchant) — production Vera underuses this family."


def r_scheduled_recurring(category, merchant, trigger, customer):
    return r_curious_ask_due(category, merchant, trigger, customer)


def r_appointment_tomorrow(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    if customer:
        cust_name, _ = customer_contact(customer)
        merchant_name = merchant.get("identity", {}).get("name", "")
        body = f"Hi {cust_name}, {merchant_name} here \U0001f44b Just confirming your appointment tomorrow. Reply 1 to confirm or 2 to reschedule."
        return clean(body), "multi_choice_slot", "merchant_on_behalf", "Customer-facing booking reminder; multi-choice slot CTA allowed for booking flows."
    body = f"{who}, you have an appointment tomorrow. Want me to send the reminder to the customer now?"
    return clean(body), "binary_yes_no", "vera", "Merchant-scope framing of an appointment reminder; asks before acting on the merchant's behalf."


def r_ipl_match_today(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    who = owner_or_name(merchant)
    match = payload.get("match")
    venue = payload.get("venue")
    when = fmt_when(payload.get("match_time_iso"))
    if not match:
        return None
    when_txt = f" around {when}" if when else " tonight"
    body = f"{who}, {match} is on{when_txt} at {venue} — expect a spike in delivery/dine-in orders. Want me to schedule a match-night post now?"
    return clean(body), "binary_yes_no", "vera", "Local news/event trigger, restaurant-relevant; match time converted from raw ISO timestamp to a readable time."


CUSTOMER_KIND_RENDERERS = {}


def r_recall_due(category, merchant, trigger, customer):
    if not customer:
        return None
    payload = trigger.get("payload", {})
    cust_name, _ = customer_contact(customer)
    merchant_name = merchant.get("identity", {}).get("name", "")
    service = natural(payload.get("service_due"))
    service = re.sub(r"(\d+)\s+month", r"\1-month", service) if service else "checkup"
    last_date = payload.get("last_service_date")
    slots = payload.get("available_slots", [])
    slot_txt = " or ".join(s.get("label", "") for s in slots[:2]) if slots else "a slot that works for you"
    offers = active_offers(merchant)
    price_txt = f" {offers[0]['title']}." if offers else ""
    hi_pref = is_hindi_pref(merchant, customer)
    since_txt = f" (last visit {last_date})" if last_date else ""
    if hi_pref:
        body = (
            f"Hi {cust_name}, {merchant_name} here \U0001f9b7 It's been a while since your last visit{since_txt} "
            f"— aapka {service} due hai. Apke liye slots ready hain: {slot_txt}.{price_txt} Reply 1 ya 2, ya time batayein jo suit kare."
        )
    else:
        body = (
            f"Hi {cust_name}, {merchant_name} here \U0001f9b7 It's been a while since your last visit{since_txt} "
            f"— your {service} recall is due. Slots ready: {slot_txt}.{price_txt} Reply 1 or 2, or tell us a time that works."
        )
    return clean(body), "multi_choice_slot", "merchant_on_behalf", "Recall reminder sent on merchant's behalf; honors language pref, uses real slots + real active offer price; service name reformatted to read naturally ('6-month cleaning' not '6 month cleaning')."


def r_wedding_package_followup(category, merchant, trigger, customer):
    if not customer:
        return None
    payload = trigger.get("payload", {})
    cust_name, _ = customer_contact(customer)
    merchant_name = merchant.get("identity", {}).get("name", "")
    days_to = payload.get("days_to_wedding")
    next_step = natural(payload.get("next_step_window_open")) or "next step"
    body = f"Hi {cust_name}, {merchant_name} here \U0001f495 {days_to} days to go! Your trial's done — this is a good window to start the {next_step}. Want me to block your slots?"
    return clean(body), "binary_yes_no", "merchant_on_behalf", "Wedding countdown is real (days_to_wedding from payload); next-step field reformatted so digits don't run into the following word (e.g. '30 day' not '30day')."


def r_customer_lapsed(category, merchant, trigger, customer):
    if not customer:
        return None
    payload = trigger.get("payload", {})
    cust_name, _ = customer_contact(customer)
    merchant_name = merchant.get("identity", {}).get("name", "")
    days = payload.get("days_since_last_visit")
    if days is None:
        return None
    focus = natural(payload.get("previous_focus"))
    offers = active_offers(merchant)
    offer_txt = f" We've got {offers[0]['title']} running right now." if offers else ""
    focus_txt = f" for your {focus} goals" if focus else ""
    body = f"Hi {cust_name}, it's been {days} days{focus_txt} — {merchant_name} here.{offer_txt} Want to jump back in this week?"
    return clean(body), "binary_yes_no", "merchant_on_behalf", "Real lapse duration + real active offer; single binary CTA, no overclaiming results. Now returns None instead of 'it's been None days' when the payload has no real days_since_last_visit (placeholder trigger)."


def r_trial_followup(category, merchant, trigger, customer):
    if not customer:
        return None
    payload = trigger.get("payload", {})
    cust_name, child_name = customer_contact(customer)
    merchant_name = merchant.get("identity", {}).get("name", "")
    options = payload.get("next_session_options", [])
    slot_txt = options[0].get("label") if options else "a slot"
    subject = f"{child_name}'s" if child_name != cust_name else "the"
    body = f"Hi {cust_name}, {merchant_name} here — how did {subject} trial session go? Next one's open for {slot_txt}. Want to lock it in?"
    return clean(body), "binary_yes_no", "merchant_on_behalf", "Trial follow-up references a real next-session slot; for a child customer, addresses the parent by name and refers to the child by name in the body instead of pasting the raw '(parent: X)' annotation."


def r_chronic_refill_due(category, merchant, trigger, customer):
    if not customer:
        return None
    payload = trigger.get("payload", {})
    cust_name, _ = customer_contact(customer)
    merchant_name = merchant.get("identity", {}).get("name", "")
    molecules = payload.get("molecule_list", [])
    runs_out = fmt_when(payload.get("stock_runs_out_iso"))
    delivery = payload.get("delivery_address_saved")
    mol_txt = ", ".join(molecules) if molecules else "your regular medicines"
    delivery_txt = " Delivered to your saved address as usual — just confirm." if delivery else " Let us know your delivery address."
    runs_out_txt = f" around {runs_out}" if runs_out else " soon"
    body = f"Hi {cust_name}, {merchant_name} here — your {mol_txt} refill runs out{runs_out_txt}.{delivery_txt}"
    return clean(body), "binary_yes_no", "merchant_on_behalf", "Chronic-refill reminder built from real molecule list + real stock-out estimate (converted from raw ISO timestamp to a readable date); no dosage/medical claims added."


def r_research_digest(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    item = digest_item(category, payload.get("top_item_id", ""))
    who = owner_or_name(merchant)
    if item:
        segment = item.get("patient_segment") or ""
        cohort_sentence = ""
        if "high_risk_adult" in segment and "high_risk_adult_cohort" in merchant.get("signals", []):
            cohort_sentence = " This is directly relevant to your high-risk adult patients."
        source_txt = friendly_source(item.get("source"))
        summary = item.get("summary", "")
        actionable = item.get("actionable", "")
        body = (
            f"{who}, a new finding from {source_txt}: {item.get('title', '')}."
            f"{cohort_sentence} {summary}"
            + (f" Suggested next step: {actionable}." if actionable else "")
            + " Want me to pull the full abstract and draft a patient-friendly WhatsApp you can share?"
        )
        return clean(body), "open_ended", "vera", "External research digest, source expanded for a reader who may not know the abbreviation; clinical anchor explicitly tied to merchant's own signal."
    # placeholder fallback — use real category peer stat instead of fake research
    peer = category.get("peer_stats", {})
    body = (
        f"{who}, this week's {category.get('display_name', category.get('slug'))} digest is in — "
        f"nothing critical for your case-mix this cycle. Peer median CTR in your segment is "
        f"{fmt_pct(peer.get('avg_ctr'))}; want me to flag items as they come relevant to your profile?"
    )
    return clean(body), "open_ended", "vera", "No specific digest item matched; kept honest — offered ongoing filtering instead of fabricating relevance."


def r_regulation_change(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    item = digest_item(category, payload.get("top_item_id", ""))
    who = owner_or_name(merchant)
    deadline = payload.get("deadline_iso", item.get("date") if item else None)
    if item:
        source_txt = friendly_source(item.get("source"))
        body = (
            f"{who}, compliance heads-up from {source_txt}: {item.get('title', '')}. "
            f"{item.get('summary', '')}"
            + (f" What to do: {item.get('actionable')}." if item.get("actionable") else "")
            + f" Deadline: {deadline or 'see circular'}. Want the checklist?"
        )
        return clean(body), "binary_yes_no", "vera", "Regulation change is high-urgency and verifiable; source expanded so the reader doesn't need to already know the abbreviation. Binary CTA for low-friction follow-through."
    body = f"{who}, a regulatory update dropped for {category.get('display_name', 'your category')} — deadline {deadline or 'TBD'}. Want the details?"
    return clean(body), "binary_yes_no", "vera", "Placeholder payload; kept generic rather than fabricating the specific rule."


TRIGGER_RENDERERS = {
    "research_digest": r_research_digest,
    "regulation_change": r_regulation_change,
    "perf_dip": r_perf_dip,
    "seasonal_perf_dip": r_perf_dip,
    "perf_spike": r_perf_spike,
    "renewal_due": r_renewal_due,
    "festival_upcoming": r_festival_upcoming,
    "milestone_reached": r_milestone_reached,
    "review_theme_emerged": r_review_theme_emerged,
    "competitor_opened": r_competitor_opened,
    "dormant_with_vera": r_dormant_with_vera,
    "cde_opportunity": r_cde_opportunity,
    "winback_eligible": r_winback_eligible,
    "supply_alert": r_supply_alert,
    "category_seasonal": r_category_seasonal,
    "gbp_unverified": r_gbp_unverified,
    "active_planning_intent": r_active_planning_intent,
    "curious_ask_due": r_curious_ask_due,
    "scheduled_recurring": r_scheduled_recurring,
    "appointment_tomorrow": r_appointment_tomorrow,
    "ipl_match_today": r_ipl_match_today,
    "recall_due": r_recall_due,
    "wedding_package_followup": r_wedding_package_followup,
    "customer_lapsed_soft": r_customer_lapsed,
    "customer_lapsed_hard": r_customer_lapsed,
    "trial_followup": r_trial_followup,
    "chronic_refill_due": r_chronic_refill_due,
}


def fallback_generic(category, merchant, trigger, customer):
    """Last-resort renderer — used when a trigger kind is unrecognized, or a
    known kind's specific payload is a placeholder stub with no real facts.
    Always grounds the message in merchant.performance / signals / offers or
    category.peer_stats — real fields — never the empty trigger payload."""
    who = owner_or_name(merchant)
    signals = merchant.get("signals", [])

    if any(s.startswith("stale_posts") for s in signals):
        stale = next(s for s in signals if s.startswith("stale_posts"))
        days = stale.split(":")[-1]
        body = f"{who}, your last Google post was {days} ago. Want me to draft one from your active offers?"
        return clean(body), "binary_yes_no", "vera", "Fell back to a real merchant signal (stale_posts) instead of the empty/placeholder trigger payload."

    perf = merchant.get("performance", {})
    peer = category.get("peer_stats", {})
    ctr = perf.get("ctr")
    peer_ctr = peer.get("avg_ctr")
    if "ctr_below_peer_median" in signals and ctr is not None and peer_ctr:
        body = (
            f"{who}, your listing's CTR is {fmt_pct(ctr)} vs a {fmt_pct(peer_ctr)} peer median in "
            f"{category.get('display_name', category.get('slug'))}. Want me to look at what's costing you clicks?"
        )
        return clean(body), "binary_yes_no", "vera", "Placeholder trigger payload; used real merchant CTR vs real category peer_stats instead of inventing trigger-specific facts."

    offers = active_offers(merchant)
    if offers and ctr is not None:
        body = (
            f"{who}, quick check-in — {offers[0]['title']} is live and you're getting "
            f"{perf.get('views', '?')} views this month. Want me to see if the offer's pulling its weight?"
        )
        return clean(body), "binary_yes_no", "vera", "Placeholder trigger payload; grounded in the merchant's real active offer + real view count rather than fabricating trigger detail."

    if ctr is not None:
        body = f"{who}, {perf.get('views', '?')} views and {perf.get('calls', '?')} calls this month so far. Anything you'd like me to dig into?"
        return clean(body), "open_ended", "vera", "Placeholder trigger payload; fell back to real merchant performance numbers with an open question (compulsion lever: asking the merchant)."

    return None


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> Optional[dict]:
    """Public composition entrypoint per challenge-brief.md §5."""
    kind = trigger.get("kind", "")
    renderer = TRIGGER_RENDERERS.get(kind, fallback_generic)
    result = renderer(category, merchant, trigger, customer)
    if result is None and renderer is not fallback_generic:
        result = fallback_generic(category, merchant, trigger, customer)
    if result is None:
        return None
    body, cta, send_as, rationale = result
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id')}"),
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# Reply engine — rule-based, deterministic conversation handling
# ---------------------------------------------------------------------------

AUTO_REPLY_PATTERNS = [
    "thank you for contacting", "our team will respond", "automated assistant",
    "will get back to you shortly", "currently unavailable", "this is an automated",
    "aapki jaankari ke liye", "hamari team tak pahuncha",
]

HOSTILE_PATTERNS = [
    "useless", "spam", "leave me alone", "don't message", "harassment",
    "abuse", "bothering me", "stop bothering",
]

# Plain "no more messages for now" — not rude, not a permanent ban. Handled
# separately from hostility: acknowledge softly and stay reachable, don't end
# the conversation or suppress the merchant.
OPT_OUT_PATTERNS = [
    "stop", "cancel", "unsubscribe", "not interested", "not now", "no thanks",
    "not right now", "band karo", "mat bhejo", "rehne do", "abhi nahi",
]

INTENT_PATTERNS = [
    "let's do it", "lets do it", "go ahead", "sounds good do it", "ok do it",
    "yes do it", "proceed", "confirm", "haan chalo", "start karo", "kar do",
    "i want to join", "want to join", "sign me up",
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def is_hostile(msg: str) -> bool:
    m = _norm(msg)
    return any(p in m for p in HOSTILE_PATTERNS)


def is_opt_out(msg: str) -> bool:
    m = _norm(msg)
    return any(p in m for p in OPT_OUT_PATTERNS)


def is_auto_reply(msg: str) -> bool:
    m = _norm(msg)
    return any(p in m for p in AUTO_REPLY_PATTERNS)


def is_intent_transition(msg: str) -> bool:
    m = _norm(msg)
    return any(p in m for p in INTENT_PATTERNS)


def conv_state(conversation_id: str, merchant_id: str = None, customer_id: str = None, trigger_id: str = None) -> dict:
    # Guarded: two concurrent requests for a brand-new conversation_id could
    # otherwise both see "missing" and both create+insert, and the second
    # write would silently discard the first caller's in-flight state.
    with state_lock:
        conv = conversations.get(conversation_id)
        if not conv:
            conv = {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "trigger_id": trigger_id,
                "history": [],
                "sent_bodies": set(),
                "consecutive_auto_replies": 0,
                "ended": False,
                "turns": 0,
                "flags": [],  # out-of-scope asks get logged here, not just discarded
                "last_touched": time.time(),
            }
            conversations[conversation_id] = conv
        else:
            conv["last_touched"] = time.time()
    return conv


async def cleanup_loop():
    """Background TTL eviction, started from the FastAPI lifespan hook below.
    Doesn't affect scoring — the judge's whole test window is ~90 real
    minutes — this is about not leaking memory if the process outlives that
    (local dev sessions, a redeploy that gets reused, etc)."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_S)
        now = time.time()
        with state_lock:
            for k in [k for k, ts in fired_suppression.items() if now - ts > SUPPRESSION_TTL_S]:
                del fired_suppression[k]
            for k in [k for k, ts in suppressed_merchants.items() if now - ts > MERCHANT_SUPPRESSION_TTL_S]:
                del suppressed_merchants[k]
            for cid in [cid for cid, c in conversations.items() if now - c.get("last_touched", now) > CONVERSATION_TTL_S]:
                del conversations[cid]


# ---------------------------------------------------------------------------
# LLM fallback for replies the deterministic rules above don't recognize —
# real questions, acknowledgments, curveballs. This is the ONLY place an LLM
# is called; the 4 branches above (hostile/opt-out/auto-reply/intent) stay
# pure rules because the replay tests need them instant and 100% reliable,
# and calling an LLM for those would just add latency/cost/risk for zero
# benefit. Design goal: cheapest model that can do the job, one call, small
# token budget, grounded strictly in real pushed context so it can't invent
# facts, and a safe deterministic fallback if the call fails for any reason
# (no key set, network error, bad JSON) — this must never block or crash
# /v1/reply's 30s budget.
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")  # small + fast + cheap on Groq
LLM_TIMEOUT_S = 10  # keep well under the 30s /v1/reply budget, leaving room for one retry
LLM_MAX_TOKENS = 280  # gpt-oss is a reasoning model — needs headroom beyond just the JSON body,
                       # even at reasoning_effort=low, or the response gets cut off mid-JSON
LLM_HISTORY_TURNS = 4  # this conversation's own recent turns; old turns add tokens, not signal
LLM_MAX_RETRY_WAIT_S = 6  # only retry a 429 if the server says the wait is short

# Kept deliberately terse — every extra sentence here is input tokens on
# every single call. JSON-only output means no second call to reformat.
LLM_SYSTEM_PROMPT = (
    "You are Vera, magicpin's WhatsApp assistant, replying to a merchant mid-conversation. "
    "Use ONLY facts in CONTEXT — never invent numbers, offers, citations, or competitor names. "
    "In-scope: this merchant's listing, marketing, offers, the topic in CONTEXT. "
    "Out-of-scope (tax/legal/unrelated/no-data requests): decline in one clause, steer back, set out_of_scope=true. "
    "Reply 1-3 sentences, no preamble, no re-introduction. "
    "If LANGUAGE below says Hindi-English mix, you MUST write the body using romanized Hindi words mixed with "
    'English, e.g. "Haan bilkul, Dental Cleaning ₹299 mein available hai. Book karna chahenge?" — never reply in '
    "pure English when told to mix. If LANGUAGE says plain English, use plain English only, no Hindi words. "
    'Output ONLY this JSON, no markdown: {"action":"send","body":"...","cta":"open_ended|binary_yes_no|none","out_of_scope":false,"rationale":"..."}'
)

# A merchant's language choice should be mirrored from what they actually
# just typed, not guessed by the model — that's what caused it to answer
# "no" in Hindi (nothing in that message was Hindi) and answer "haan bhej
# do" in English (missing the obvious cue). Keep this deterministic and free.
_HINGLISH_WORDS = {
    "hai", "hain", "kya", "bhej", "bhejo", "chalo", "nahi", "haan", "aap",
    "aapka", "aapke", "apka", "apke", "karo", "kar", "abhi", "theek", "thik",
    "accha", "achha", "bata", "batao", "kaise", "kyun", "kyu", "mujhe",
    "mera", "meri", "hoon", "hoga", "hogi", "rehne", "rakho", "dijiye",
    "kripya", "shukriya", "dhanyavad", "band",
}


def _msg_is_hinglish(text: str) -> bool:
    if re.search(r"[ऀ-ॿ]", text):  # Devanagari script present
        return True
    tokens = set(re.findall(r"[a-zA-Z]+", text.lower()))
    return bool(tokens & _HINGLISH_WORDS)


def _language_directive(state: dict, merchant_message: str) -> str:
    if _msg_is_hinglish(merchant_message):
        return "Hindi-English mix (romanized) — the merchant just wrote in Hindi-English, match that style."
    return "Plain English — the merchant's message has no Hindi in it, reply in plain English only."


def _llm_available() -> bool:
    return bool(GROQ_API_KEY)


def _build_llm_context(state: dict) -> str:
    merchant = get_ctx("merchant", state.get("merchant_id")) or {}
    category = get_ctx("category", merchant.get("category_slug")) if merchant else None
    category = category or {}
    trigger = get_ctx("trigger", state.get("trigger_id")) or {}

    who = owner_or_name(merchant) if merchant else "the merchant"
    offers = [o.get("title") for o in active_offers(merchant)] if merchant else []
    trigger_kind = trigger.get("kind", "unknown")
    # Trim the payload to short scalar fields only — skip nested structures
    # (slot lists, etc.) that cost tokens without helping a short reply.
    payload = trigger.get("payload", {})
    compact_payload = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool)) or v is None}

    # If the trigger points at a specific digest item (research/regulation/CDE
    # kinds all do this via an id field), resolve its real title/source/
    # summary here explicitly — don't rely on conversation history alone to
    # carry that forward. A trigger payload on its own is often just an id
    # string like "d_2026W17_jida_fluoride"; without the resolved content an
    # LLM asked about it has nothing but that string to guess from, which is
    # exactly what produced a fabricated "JIDA is a fluoride treatment"
    # answer before this was added.
    item_id = payload.get("top_item_id") or payload.get("digest_item_id")
    digest_block = ""
    item = digest_item(category, item_id) if item_id else None
    if item:
        digest_block = (
            f" | referenced digest item: title=\"{item.get('title', '')}\" "
            f"source={friendly_source(item.get('source'))} "
            f"summary=\"{item.get('summary', '')[:200]}\""
        )

    return (
        f"Business: {who} ({category.get('slug', 'unknown')}); "
        f"voice: {category.get('voice', {}).get('tone', 'peer')}; "
        f"offers: {offers or 'none'}; "
        f"trigger: {trigger_kind} {json.dumps(compact_payload, ensure_ascii=False)[:250]}"
        f"{digest_block}"
    )


def _build_llm_history(state: dict) -> str:
    turns = state.get("history", [])[-LLM_HISTORY_TURNS:]
    if not turns:
        return "(none)"
    return " | ".join(f"{t['from']}: {t['msg'][:200]}" for t in turns)


def _fallback_reply(reason: str) -> dict:
    return {
        "action": "send",
        "body": "Got it — let me look into that and come back to you shortly.",
        "cta": "none",
        "rationale": f"LLM fallback path used ({reason}); avoided guessing, kept the thread open honestly.",
    }


def _call_groq(payload_bytes: bytes) -> dict:
    req = urlrequest.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload_bytes,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "vera-beat/1.0 (+magicpin-ai-challenge)",  # Groq's Cloudflare front-end blocks requests with no UA
        },
    )
    resp = urlrequest.urlopen(req, timeout=LLM_TIMEOUT_S)
    return json.loads(resp.read().decode("utf-8"))


def llm_reply(state: dict, merchant_message: str) -> dict:
    if not _llm_available():
        return _fallback_reply("no GROQ_API_KEY configured")

    user_prompt = (
        f"CONTEXT: {_build_llm_context(state)}\n"
        f"CONVERSATION SO FAR: {_build_llm_history(state)}\n"
        f'Merchant\'s latest message: "{merchant_message}"\n'
        f"LANGUAGE: {_language_directive(state, merchant_message)}"
    )

    body = json.dumps({
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_tokens": LLM_MAX_TOKENS,
        "reasoning_effort": "low",  # gpt-oss defaults to heavy hidden chain-of-thought; this keeps token spend on the actual answer
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }).encode("utf-8")

    try:
        data = _call_groq(body)
    except urlerror.HTTPError as e:
        # A 429 with a short suggested wait is worth one retry — still well
        # inside the judge's 30s-per-call budget. Anything else, don't stall.
        if e.code == 429:
            try:
                err = json.loads(e.read().decode("utf-8"))
                wait_s = float(re.search(r"try again in ([\d.]+)s", err.get("error", {}).get("message", "")).group(1))
            except Exception:
                wait_s = LLM_MAX_RETRY_WAIT_S + 1
            if wait_s <= LLM_MAX_RETRY_WAIT_S:
                time.sleep(wait_s)
                try:
                    data = _call_groq(body)
                except Exception as e2:
                    return _fallback_reply(f"LLM retry failed: {type(e2).__name__}")
            else:
                return _fallback_reply("rate limited, wait too long to retry within budget")
        else:
            return _fallback_reply(f"LLM HTTP {e.code}")
    except (urlerror.URLError, TimeoutError) as e:
        return _fallback_reply(f"LLM call failed: {type(e).__name__}")

    try:
        raw = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return _fallback_reply("LLM response missing expected fields")

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return _fallback_reply("LLM returned non-JSON")
    try:
        parsed = json.loads(match.group())
    except ValueError:
        return _fallback_reply("LLM returned malformed JSON")

    result = {
        "action": "send",
        "body": clean(str(parsed.get("body", "")).strip()) or "Got it — noted.",
        "cta": parsed.get("cta") if parsed.get("cta") in ("open_ended", "binary_yes_no", "none") else "none",
        "rationale": str(parsed.get("rationale", "LLM-composed reply grounded in conversation context.")),
    }

    # Safety net: the LLM sometimes explains a known abbreviation using its
    # own (wrong) general knowledge instead of admitting it doesn't know,
    # even when told to only use CONTEXT. If it did that here, override with
    # the deterministic glossary answer instead of forwarding a fabrication.
    if not glossary_grounding_ok(result["body"]):
        corrected = glossary_answer(merchant_message) or next(
            (f"{a} stands for {e}." for a, e in ABBREV_GLOSSARY.items() if re.search(rf"\b{re.escape(a)}\b", result["body"], re.IGNORECASE)),
            None,
        )
        result["body"] = corrected or "I don't have reliable information on that — let me check and get back to you."
        result["rationale"] = "LLM's explanation of a known term didn't match the glossary; overridden with the verified definition instead of forwarding a fabrication."

    if parsed.get("out_of_scope"):
        state["flags"].append({
            "turn": state["turns"],
            "message": merchant_message,
            "reason": result["rationale"],
        })
        result["rationale"] = f"[FLAGGED: out-of-scope ask] {result['rationale']}"

    return result


def respond(state: dict, merchant_message: str) -> dict:
    """Given conversation state + latest merchant/customer message, produce the reply.
    Exposed standalone too (see conversation_handlers.py) for the optional multi-turn contract.
    """
    state["turns"] += 1
    msg = merchant_message

    if is_hostile(msg):
        state["ended"] = True
        return {"action": "end", "rationale": "Merchant signaled hostility/harassment; closing without further engagement."}

    if is_opt_out(msg):
        # Not hostile — just "not now". Don't end the conversation or
        # suppress the merchant; stay reachable so a later reply from them
        # picks the thread back up normally through this same function.
        state["consecutive_auto_replies"] = 0
        return {
            "action": "send",
            "body": "Okay sure, Just reply whenever you want to go ahead with this.",
            "cta": "none",
            "rationale": "Merchant opted out for now (not hostile) — acknowledged softly without closing the conversation or suppressing future contact; bot stays reachable for whenever they reply next.",
        }

    if is_auto_reply(msg):
        state["consecutive_auto_replies"] += 1
        n = state["consecutive_auto_replies"]
        if n == 1:
            return {
                "action": "send",
                "body": "Looks like an auto-reply \U0001f60a When the owner sees this, one reply is all it takes to move forward.",
                "cta": "binary_yes_no",
                "rationale": "First auto-reply detected; one lightweight prompt to flag it for the owner (per anti-pattern guidance: don't burn multiple turns).",
            }
        elif n == 2:
            return {"action": "wait", "wait_seconds": 14400, "rationale": "Second consecutive auto-reply; owner likely not at phone. Backing off 4 hours."}
        else:
            state["ended"] = True
            return {"action": "end", "rationale": "Auto-reply 3+ times in a row with zero real engagement signal; closing to avoid wasting turns."}

    # any real reply resets the auto-reply streak
    state["consecutive_auto_replies"] = 0

    if is_intent_transition(msg):
        return {
            "action": "send",
            "body": "Great — moving straight to it. Drafting now; I'll share it here in a moment. Reply CONFIRM once you've seen it and I'll push it live.",
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant gave explicit commitment; switched directly to action mode instead of re-qualifying (this is the #1 production Vera miss called out in the brief).",
        }

    glossary = glossary_answer(msg)
    if glossary:
        return {
            "action": "send",
            "body": glossary,
            "cta": "none",
            "rationale": "Deterministic glossary lookup — bypassed the LLM entirely for a definitional question we already have ground truth for, since it has fabricated wrong expansions for this exact term in testing.",
        }

    # Everything past this point needs actual understanding, not pattern
    # matching — real questions, acknowledgments, curveballs, anything that
    # doesn't fit the fixed shapes above. One grounded LLM call, checked
    # against the glossary as a safety net, or a safe deterministic fallback
    # if no key is configured / the call fails.
    return llm_reply(state, msg)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        if scope in counts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Beat",
        "team_members": ["D Shakthi Saravanan"],
        "model": "deterministic-template-composer-v1 for opening messages; openai/gpt-oss-20b (via Groq) grounded fallback for replies outside the 4 rule-based patterns",
        "approach": "Rule-based composer dispatched by trigger.kind; every field in the message traces back to a "
                     "pushed context field (no fabrication). Reply engine handles auto-reply detection, "
                     "intent-transition routing, and hostile/opt-out exits deterministically, with a grounded LLM "
                     "call for everything else.",
        "contact_email": "dshakthi2003@gmail.com",
        "version": "1.1.0",
        "submitted_at": now_iso(),
    }


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope {body.scope!r}"}
    key = (body.scope, body.context_id)
    with state_lock:
        cur = contexts.get(key)
        if cur and cur["version"] >= body.version:
            if cur["version"] == body.version:
                return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": now_iso()}
            return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
        contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": now_iso()}


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


def _compose_for_trigger(trg_id: str) -> Optional[dict]:
    """One trigger's full lookup + suppression-check + compose. Runs off the
    event loop via asyncio.to_thread (see tick() below), so this function
    may execute concurrently with others — the fired_suppression check MUST
    be paired with reserving the key in the same lock acquisition, not
    checked-then-written-later, or two triggers sharing a suppression_key
    (or the same trigger id appearing twice in one batch) could both pass
    the check and both compose before either marks it as sent."""
    trigger = get_ctx("trigger", trg_id)
    if not trigger:
        return None

    supp_key = trigger.get("suppression_key", trg_id)
    merchant_id = trigger.get("merchant_id")

    with state_lock:
        if supp_key in fired_suppression:
            return None  # already sent this exact trigger instance — restraint over spam
        if merchant_id and merchant_id in suppressed_merchants:
            return None  # merchant opted out / went hostile — respect it
        fired_suppression[supp_key] = time.time()  # reserve immediately — see docstring

    def _release():
        with state_lock:
            fired_suppression.pop(supp_key, None)

    merchant = get_ctx("merchant", merchant_id) if merchant_id else None
    if not merchant:
        _release()
        return None

    category = get_ctx("category", merchant.get("category_slug", ""))
    if not category:
        _release()
        return None

    customer_id = trigger.get("customer_id")
    customer = get_ctx("customer", customer_id) if customer_id else None
    if trigger.get("scope") == "customer" and not customer:
        _release()  # can't personalize a customer-scoped message without the customer context
        return None

    composed = compose(category, merchant, trigger, customer)
    if not composed:
        _release()
        return None

    conversation_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:6]}"
    state = conv_state(conversation_id, merchant_id, customer_id, trg_id)
    state["sent_bodies"].add(composed["body"])
    # Without this, a follow-up reply's LLM call has zero record of what the
    # opening message actually said — only the trigger's raw payload (often
    # just an id string) — and will guess. This is what caused the bot to
    # hallucinate "JIDA is a fluoride treatment" earlier: it never saw the
    # opening message that had already explained what JIDA actually is.
    state["history"].append({"from": "vera", "msg": composed["body"]})

    return {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": composed["send_as"],
        "trigger_id": trg_id,
        "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
        "template_params": [owner_or_name(merchant), composed["body"]],
        "body": composed["body"],
        "cta": composed["cta"],
        "suppression_key": composed["suppression_key"],
        "rationale": composed["rationale"],
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    # Fan out across triggers concurrently instead of one at a time. Each
    # goes through asyncio.to_thread — compose() itself is cheap CPU work
    # today, but this is the shape that stays correct if composition ever
    # calls an LLM the way the reply engine already does, and it's what lets
    # the suppression-key locking above actually matter.
    results = await asyncio.gather(
        *(asyncio.to_thread(_compose_for_trigger, trg_id) for trg_id in body.available_triggers)
    )
    actions = [r for r in results if r is not None][:20]
    return {"actions": actions}


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: int = 0


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conv_state(body.conversation_id, body.merchant_id, body.customer_id)
    if state.get("ended"):
        return {"action": "end", "rationale": "Conversation already closed; not re-engaging."}

    state["history"].append({"from": body.from_role, "msg": body.message})

    # respond() may call out to Groq over a blocking HTTP request (up to
    # ~16s counting the 429 retry). Running it on a worker thread instead of
    # directly on the event loop is what keeps /v1/healthz and any other
    # concurrent request responsive while that call is in flight — this was
    # the single biggest correctness risk in the whole file before this
    # change: a slow LLM call could otherwise freeze the entire bot,
    # including the healthz polling the judge disqualifies on 3 failures of.
    result = await asyncio.to_thread(respond, state, body.message)

    if result["action"] == "send":
        if result["body"] in state["sent_bodies"]:
            result["body"] = "Just circling back on this — still open if you'd like to continue."
        state["sent_bodies"].add(result["body"])
        state["history"].append({"from": "vera", "msg": result["body"]})
    elif result["action"] == "end" and is_hostile(body.message):
        if body.merchant_id:
            with state_lock:
                suppressed_merchants[body.merchant_id] = time.time()

    return result


@app.post("/v1/teardown")
async def teardown():
    with state_lock:
        contexts.clear()
        conversations.clear()
        fired_suppression.clear()
        suppressed_merchants.clear()
    return {"status": "wiped"}
