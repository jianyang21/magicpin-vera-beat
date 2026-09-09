import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent))
from bot import compose  # noqa: E402

DATASET = Path(__file__).parent.parent / "dataset" / "expanded"
categories = {d["slug"]: d for d in (json.load(open(f, encoding="utf-8")) for f in (DATASET / "categories").glob("*.json"))}
merchants = {d["merchant_id"]: d for d in (json.load(open(f, encoding="utf-8")) for f in (DATASET / "merchants").glob("*.json"))}
customers = {d["customer_id"]: d for d in (json.load(open(f, encoding="utf-8")) for f in (DATASET / "customers").glob("*.json"))}
triggers = {t["id"]: t for t in (json.load(open(f, encoding="utf-8")) for f in (DATASET / "triggers").glob("*.json"))}

check = [
    "trg_014_seasonal_acquisition_dip_powerhouse", "trg_071_customer_lapsed_soft_m_014_dr_asha_dentis",
    "trg_010_ipl_match_delhi", "trg_019_chronic_refill_grandfather", "trg_012_milestone_mylari",
    "trg_021_unverified_gbp_sunrise", "trg_007_bridal_followup_kavya", "trg_017_kids_yoga_trial_followup_karthik",
    "trg_022_cde_webinar_dentists",
]
for tid in check:
    t = triggers[tid]
    m = merchants.get(t["merchant_id"])
    cat = categories.get(m.get("category_slug"))
    cust = customers.get(t.get("customer_id")) if t.get("customer_id") else None
    r = compose(cat, m, t, cust)
    print(f"--- {t['kind']} ({tid}) ---")
    print(r["body"] if r else "DECLINED")
    print()
