import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from bot import respond, conv_state  # noqa: E402

s1 = conv_state("conv_test_stop")
r1 = respond(s1, "stop")
print('1) message="stop" ->', r1["action"], "| ended flag:", s1["ended"])
print("   body:", r1.get("body"))

r2 = respond(s1, "ok yes lets do it")
print("2) follow-up after opt-out ->", r2["action"])
print("   body:", r2.get("body"))

s3 = conv_state("conv_test_hostile")
r3 = respond(s3, "This is useless spam, leave me alone")
print("3) hostile ->", r3["action"], "| ended flag:", s3["ended"])

s4 = conv_state("conv_test_notinterested")
r4 = respond(s4, "not interested")
print("4) not interested ->", r4["action"], "| ended flag:", s4["ended"])
print("   body:", r4.get("body"))
