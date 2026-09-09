import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
import bot  # noqa: E402

DATASET = Path("../dataset/expanded")
category = json.load(open(DATASET / "categories/dentists.json", encoding="utf-8"))
merchant = json.load(open(DATASET / "merchants/m_001_drmeera_dentist_delhi.json", encoding="utf-8"))
trigger = json.load(open(DATASET / "triggers/trg_001_research_digest_dentists.json", encoding="utf-8"))

bot.contexts[("category", "dentists")] = {"version": 1, "payload": category}
bot.contexts[("merchant", merchant["merchant_id"])] = {"version": 1, "payload": merchant}
bot.contexts[("trigger", trigger["id"])] = {"version": 1, "payload": trigger}

opening = bot.compose(category, merchant, trigger, None)["body"]

messages = [
    "yes send it",
    "sure",
    "haan bhej do",
    "what is JIDA",
    "can you also help with my GST filing",
    "not interested right now",
    "ok let's do it",
    "why are you like this, stop it",
    "thanks",
    "hi",
    "how much does the abstract cost",
    "Thank you for contacting us! Our team will respond shortly.",
    "no",
    "yes",
    "can you explain that again in simple terms",
    "who is this",
]

print(f"[opening message the bot sent] {opening}\n")
for i, msg in enumerate(messages):
    s = bot.conv_state(f"conv_ctx_{i}", merchant["merchant_id"], None, trigger["id"])
    s["history"].append({"from": "vera", "msg": opening})
    s["history"].append({"from": "merchant", "msg": msg})
    r = bot.respond(s, msg)
    body = r.get("body", f"[{r['action']}]")
    flag = "  <<< FLAGGED" if s["flags"] else ""
    print(f"USER: {msg}")
    print(f"BOT ({r['action']}): {body}{flag}")
    print(f"   rationale: {r.get('rationale')}")
    print()
