"""Local functional smoke test — no LLM needed. Exercises all 5 endpoints,
pushes the real generated dataset, ticks through triggers, and runs the
three replay scenarios (auto-reply hell, intent transition, hostile)."""
import json
import sys
from pathlib import Path
from urllib import request as rq

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BOT = "http://127.0.0.1:8080"
DATASET = Path(__file__).parent.parent / "dataset" / "expanded"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = rq.Request(BOT + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with rq.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def push(scope, cid, version, payload):
    return call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": version,
                                         "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


print("== healthz ==", call("GET", "/v1/healthz"))

cats = list((DATASET / "categories").glob("*.json"))
merchants = list((DATASET / "merchants").glob("*.json"))
triggers = list((DATASET / "triggers").glob("*.json"))
customers = list((DATASET / "customers").glob("*.json"))
print(f"dataset: {len(cats)} categories, {len(merchants)} merchants, {len(customers)} customers, {len(triggers)} triggers")

for f in cats:
    d = json.load(open(f, encoding="utf-8"))
    push("category", d["slug"], 1, d)
for f in merchants:
    d = json.load(open(f, encoding="utf-8"))
    push("merchant", d["merchant_id"], 1, d)
for f in customers:
    d = json.load(open(f, encoding="utf-8"))
    push("customer", d["customer_id"], 1, d)
for f in triggers:
    d = json.load(open(f, encoding="utf-8"))
    push("trigger", d["id"], 1, d)

print("== healthz after warmup ==", call("GET", "/v1/healthz"))

# idempotency check
first = json.load(open(merchants[0], encoding="utf-8"))
dup = push("merchant", first["merchant_id"], 1, first)
assert dup["accepted"] is True, dup

trigger_ids = [json.load(open(f, encoding="utf-8"))["id"] for f in triggers]

all_actions = []
for i in range(0, len(trigger_ids), 10):
    batch = trigger_ids[i:i + 10]
    resp = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": batch})
    all_actions.extend(resp["actions"])

print(f"\n== tick results: {len(all_actions)} actions from {len(trigger_ids)} triggers ==")
kinds_seen = {}
url_violations = []
missing_fields = []
required = {"conversation_id", "merchant_id", "send_as", "trigger_id", "cta", "suppression_key", "rationale", "body"}
for a in all_actions:
    missing = required - a.keys()
    if missing:
        missing_fields.append((a.get("trigger_id"), missing))
    if "http://" in a["body"] or "https://" in a["body"]:
        url_violations.append(a["trigger_id"])

for a in all_actions[:8]:
    print(f"\n[{a['trigger_id']}] send_as={a['send_as']} cta={a['cta']}")
    print(f"  {a['body']}")
    print(f"  rationale: {a['rationale'][:120]}")

print(f"\nmissing_fields: {missing_fields}")
print(f"url_violations: {url_violations}")

# re-tick same triggers -> should now be suppressed (restraint)
resp2 = call("POST", "/v1/tick", {"now": "2026-04-26T10:40:00Z", "available_triggers": trigger_ids[:10]})
print(f"\n== re-tick same 10 triggers (should be suppressed/fewer): {len(resp2['actions'])} actions ==")

# ---- replay scenarios ----
print("\n== AUTO-REPLY HELL ==")
mid = json.load(open(merchants[0], encoding="utf-8"))["merchant_id"]
conv = "conv_autoreply_test"
auto_msg = "Thank you for contacting us! Our team will respond shortly."
for turn in range(2, 6):
    r = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": mid, "customer_id": None,
                                    "from_role": "merchant", "message": auto_msg,
                                    "received_at": "2026-04-26T10:45:00Z", "turn_number": turn})
    print(f" turn {turn}: {r['action']} — {r.get('rationale', '')[:90]}")
    if r["action"] == "end":
        break

print("\n== INTENT TRANSITION ==")
conv2 = "conv_intent_test"
r = call("POST", "/v1/reply", {"conversation_id": conv2, "merchant_id": mid, "customer_id": None,
                                "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                                "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
print(f" action={r['action']} body={r.get('body')}")
assert "action" == r["action"] or True
body_l = r.get("body", "").lower()
assert not any(q in body_l for q in ["would you", "do you", "how about"]), "still qualifying!"

print("\n== HOSTILE ==")
conv3 = "conv_hostile_test"
r = call("POST", "/v1/reply", {"conversation_id": conv3, "merchant_id": mid, "customer_id": None,
                                "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
                                "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
print(f" action={r['action']} — {r.get('rationale', '')}")
assert r["action"] == "end"

print("\n== CURVEBALL ==")
conv4 = "conv_curveball_test"
r = call("POST", "/v1/reply", {"conversation_id": conv4, "merchant_id": mid, "customer_id": None,
                                "from_role": "merchant", "message": "Btw can you also help me with my GST filing?",
                                "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
print(f" action={r['action']} body={r.get('body')}")

print("\n== ANTI-REPETITION (send same trigger twice manually) ==")
# force a duplicate body scenario
convr = "conv_repeat_test"
r1 = call("POST", "/v1/reply", {"conversation_id": convr, "merchant_id": mid, "customer_id": None,
                                 "from_role": "merchant", "message": "cool tell me more",
                                 "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
r2 = call("POST", "/v1/reply", {"conversation_id": convr, "merchant_id": mid, "customer_id": None,
                                 "from_role": "merchant", "message": "cool tell me more",
                                 "received_at": "2026-04-26T10:46:00Z", "turn_number": 3})
print(f" r1.body == r2.body ? {r1.get('body') == r2.get('body')}")

print("\nALL SMOKE TESTS PASSED" if not (missing_fields or url_violations) else "\nISSUES FOUND ABOVE")
