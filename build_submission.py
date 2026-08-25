"""Build submission.jsonl from the 30 canonical test pairs, calling the
same `compose()` used by /v1/tick — so the JSONL and the live endpoint
never disagree."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot import compose  # noqa: E402

ROOT = Path(__file__).parent.parent
DATASET = ROOT / "dataset" / "expanded"


def load_all(subdir, key):
    out = {}
    for f in (DATASET / subdir).glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        out[d[key]] = d
    return out


categories = {d["slug"]: d for d in (json.load(open(f, encoding="utf-8")) for f in (DATASET / "categories").glob("*.json"))}
merchants = load_all("merchants", "merchant_id")
customers = load_all("customers", "customer_id")
triggers = load_all("triggers", "id")

pairs = json.load(open(DATASET / "test_pairs.json", encoding="utf-8"))["pairs"]

lines = []
skipped = []
for p in pairs:
    trigger = triggers.get(p["trigger_id"])
    merchant = merchants.get(p["merchant_id"])
    customer = customers.get(p["customer_id"]) if p.get("customer_id") else None
    if not trigger or not merchant:
        skipped.append(p["test_id"])
        continue
    category = categories.get(merchant.get("category_slug"))
    composed = compose(category, merchant, trigger, customer)
    if not composed:
        skipped.append(p["test_id"])
        continue
    lines.append({
        "test_id": p["test_id"],
        "body": composed["body"],
        "cta": composed["cta"],
        "send_as": composed["send_as"],
        "suppression_key": composed["suppression_key"],
        "rationale": composed["rationale"],
    })

out_path = Path(__file__).parent / "submission.jsonl"
with open(out_path, "w", encoding="utf-8") as f:
    for line in lines:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")

print(f"Wrote {len(lines)} lines to {out_path}")
if skipped:
    print(f"Skipped (no composable message — see rationale in code comments): {skipped}")
