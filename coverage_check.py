import json
import sys
from pathlib import Path
from urllib import request as rq
import collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
BOT = os.environ.get("BOT_URL", "http://127.0.0.1:8080")
DATASET = Path(__file__).parent.parent / "dataset" / "expanded"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = rq.Request(BOT + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with rq.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def push(scope, cid, version, payload):
    return call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": version,
                                         "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


for f in (DATASET / "categories").glob("*.json"):
    d = json.load(open(f, encoding="utf-8"))
    push("category", d["slug"], 1, d)
for f in (DATASET / "merchants").glob("*.json"):
    d = json.load(open(f, encoding="utf-8"))
    push("merchant", d["merchant_id"], 1, d)
for f in (DATASET / "customers").glob("*.json"):
    d = json.load(open(f, encoding="utf-8"))
    push("customer", d["customer_id"], 1, d)

trigger_files = list((DATASET / "triggers").glob("*.json"))
trigger_data = {}
for f in trigger_files:
    d = json.load(open(f, encoding="utf-8"))
    trigger_data[d["id"]] = d
    push("trigger", d["id"], 1, d)

hit_ids = set()
for i in range(0, len(trigger_data), 10):
    batch = list(trigger_data.keys())[i:i + 10]
    resp = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": batch})
    for a in resp["actions"]:
        hit_ids.add(a["trigger_id"])

miss_ids = set(trigger_data.keys()) - hit_ids
by_kind_miss = collections.Counter(trigger_data[i]["kind"] for i in miss_ids)
print(f"hit={len(hit_ids)} miss={len(miss_ids)}")
print("misses by kind:", dict(by_kind_miss))
for i in list(miss_ids)[:10]:
    t = trigger_data[i]
    print(f"  MISS {i} kind={t['kind']} scope={t['scope']} customer_id={t.get('customer_id')} payload={t.get('payload')}")
