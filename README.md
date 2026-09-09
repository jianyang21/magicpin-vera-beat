# Vera Beat — submission README

## See it live

The bot is deployed at **https://magicpin-vera-beat.onrender.com**. Since
it's a plain JSON API (no homepage), the easiest way to see it working
without writing any code is the interactive API docs FastAPI generates
automatically:

**https://magicpin-vera-beat.onrender.com/docs**

That page lists all 5 endpoints, lets you fill in a request body in the
browser, and shows the live response — a good starting point before writing
any client code against it.

## Approach

Two layers, split by how safety-critical they are:

**Opening messages** (`/v1/tick`) are a deterministic, template-driven
composer — no LLM in this path. Every `trigger.kind` dispatches to a small
renderer function that pulls fields straight from the pushed `category` /
`merchant` / `trigger` / `customer` context objects. If a field isn't
there, the renderer either falls back to a different *real* field (e.g. a
generated trigger with a `{"placeholder": true}` payload falls back to the
merchant's real `performance.delta_7d` or `signals`) or declines to send.
Nothing is ever invented — no fake citations, no fake competitor names, no
fake numbers.

**Replies** (`/v1/reply`, `respond()` in `bot.py`) are a hybrid:

- **Auto-reply detection**: phrase-matches canned WhatsApp Business replies.
  1st occurrence → one lightweight nudge. 2nd → back off 4h. 3rd+ → end.
- **Intent transition**: keyword-matches explicit commitment ("let's do it",
  "go ahead", "sign me up") and routes straight to action mode instead of
  re-qualifying — this is the #1 miss called out for production Vera.
- **Hostile**: ends immediately and suppresses the merchant for the rest of
  the session.
- **Soft opt-out** ("stop", "not interested"): acknowledged gently, stays
  reachable — doesn't end the conversation or suppress the merchant, so a
  later reply from them picks the thread back up normally.
- **Everything else** (real questions, acknowledgments, curveballs) goes to
  a single grounded LLM call (Groq, `openai/gpt-oss-20b`, `temperature=0`,
  `reasoning_effort=low`) — given only the real trigger/merchant/category
  context plus this conversation's own turns, told to answer only from
  what's there, and to flag + decline anything outside its job (tax/legal
  advice, unrelated requests). If no `GROQ_API_KEY` is set or the call
  fails for any reason, it falls back to a safe deterministic reply rather
  than crashing or stalling the request.
- **Anti-repetition**: every conversation tracks bodies already sent; a
  literal repeat is swapped for a short follow-up line instead.

The 4 rule-based branches above stay pure rules on purpose — the replay
tests need them instant and 100% reliable, and an LLM adds latency/cost/risk
there for no benefit. The LLM is scoped narrowly to the bucket that actually
needs real language understanding.

## Why deterministic-first for composition

The brief explicitly rewards restraint over spam and penalizes
hallucination harder than it rewards cleverness. A rule-based composer
can't hallucinate — every clause in every opening message traces back to a
specific field in a specific pushed payload. The `rationale` field on every
action says exactly which real field the message is anchored on, which
should make it easy for the judge (or a human) to audit.

## Concurrency & reliability

- `/v1/tick` fans out trigger composition concurrently (`asyncio.gather` +
  `asyncio.to_thread`) instead of processing triggers one at a time.
- `/v1/reply`'s LLM call runs via `asyncio.to_thread` — a slow or stuck Groq
  call can no longer freeze the whole server (verified: `/v1/healthz`
  responds in <10ms even while a reply is blocked on a simulated 5s LLM
  call — see `blocking_test.py`).
- All shared-state check-then-write sequences (suppression keys, merchant
  suppression, new conversation creation) are guarded by a `threading.Lock`
  — verified against real concurrent load: firing the same trigger 15x in
  one tick batch, or across 10 concurrent separate tick requests, both
  produce exactly one action, not duplicates.
- A background task prunes suppression keys, merchant suppressions, and
  ended conversations older than their TTL, so memory doesn't grow
  unbounded over a long-running process.
- **This only works as a single worker process** — all state is in-process
  memory by design (no Redis/Postgres). `render.yaml` pins `--workers 1`
  explicitly; running multiple workers would silently break idempotency and
  suppression since each worker would hold its own separate state.

## What's implemented

- All 5 endpoints (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`,
  `/v1/metadata`), plus an optional `/v1/teardown`.
- Idempotent context storage by `(scope, context_id, version)`.
- 26 trigger-kind renderers (every kind in the generated dataset) plus a
  generic fallback that still grounds in real merchant/category fields.
- Suppression-key dedup (won't refire the same trigger instance) and
  per-merchant hard suppression after a hostile reply.
- All 3 replay scenarios (auto-reply hell, intent transition, hostile)
  verified locally — see `local_test.py`.
- `submission.jsonl` — 30/30 canonical test pairs, generated by calling the
  exact same `compose()` the live `/v1/tick` endpoint uses (`build_submission.py`),
  so the file and the endpoint can never drift apart.

## Tradeoffs

- The LLM reply path depends on Groq's free-tier rate limit (8000
  tokens/min on the tier this was built against) — a burst of many replies
  in a short window could hit a 429; one short retry is built in, beyond
  that it degrades to the deterministic fallback rather than stalling.
- Trigger kinds with only a placeholder payload (about a quarter of the
  generated dataset) can't produce kind-specific copy since there's no real
  fact to anchor on — the fallback intentionally trades specificity for
  honesty in those cases rather than fabricating.
- Hindi-English code-mixing in replies is decided per-message from the
  merchant's actual latest text (a small deterministic detector), not a
  stored preference — mirrors what they just wrote rather than guessing
  from history.

## What additional context would have helped most

- A `last_bot_message` / `open_topic` field in `/v1/reply` payloads (instead
  of inferring it purely from local conversation state) would make the
  curveball-redirect case ("...coming back to X") more precise.
- Real digest/competitor/review-theme data for the placeholder-payload
  triggers — right now those are indistinguishable from "trigger has no
  useful information," which caps specificity on a portion of the 100
  sample triggers.

## Running locally

```bash
pip install -r requirements.txt
export GROQ_API_KEY="..."   # optional — omit to get deterministic fallback replies
uvicorn bot:app --host 0.0.0.0 --port 8080
```

`local_test.py` pushes the full generated dataset (`../dataset/expanded`)
and exercises tick/reply/replay scenarios end-to-end without needing any
LLM API key. `coverage_check.py` reports which trigger kinds fire vs.
abstain. `build_submission.py` regenerates `submission.jsonl`.
`blocking_test.py` proves `/v1/healthz` stays responsive during a slow
simulated LLM call.
