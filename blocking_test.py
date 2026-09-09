"""Proves /v1/healthz stays responsive while /v1/reply is stuck on a slow
(simulated) LLM call. Spins up its own throwaway uvicorn instance with
llm_reply patched to sleep 5s, so it doesn't touch the dev server on 8080."""
import sys
import time
import threading
import json
import concurrent.futures
from urllib import request as rq

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

import bot  # noqa: E402

SLEEP_S = 5


def slow_llm_reply(state, msg):
    time.sleep(SLEEP_S)
    return {"action": "send", "body": "(slow simulated LLM reply)", "cta": "none", "rationale": "test"}


bot.llm_reply = slow_llm_reply  # patch before the server starts serving

import uvicorn  # noqa: E402

PORT = 8099
config = uvicorn.Config(bot.app, host="127.0.0.1", port=PORT, log_level="warning")
server = uvicorn.Server(config)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()

# wait for boot
for _ in range(50):
    try:
        rq.urlopen(f"http://127.0.0.1:{PORT}/v1/healthz", timeout=1)
        break
    except Exception:
        time.sleep(0.1)

BOT = f"http://127.0.0.1:{PORT}"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = rq.Request(BOT + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with rq.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def do_slow_reply():
    t0 = time.time()
    call("POST", "/v1/reply", {
        "conversation_id": "conv_slow_test", "merchant_id": "m_test", "customer_id": None,
        "from_role": "merchant", "message": "some real question needing the LLM path",
        "received_at": "2026-01-01T00:00:00Z", "turn_number": 2,
    })
    return time.time() - t0


def do_healthz_during():
    time.sleep(0.5)  # let the slow reply start first
    t0 = time.time()
    call("GET", "/v1/healthz")
    return time.time() - t0


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
    f_reply = ex.submit(do_slow_reply)
    f_health = ex.submit(do_healthz_during)
    reply_latency = f_reply.result()
    health_latency = f_health.result()

print(f"/v1/reply (simulated {SLEEP_S}s LLM call) took: {reply_latency:.2f}s")
print(f"/v1/healthz fired DURING that call took: {health_latency:.2f}s")

if health_latency < 1.0:
    print("PASS — healthz stayed responsive while the LLM call was in flight")
else:
    print("FAIL — healthz was blocked by the in-flight LLM call")

server.should_exit = True
thread.join(timeout=3)
