#!/usr/bin/env python3
"""Throughput rehearsal: send N copies of a fixture with K in flight and report timings.

Usage: python3 scripts/load-test.py data/fixtures/f1-durand-clean-order.json --count 50 --concurrency 10 [--timeout 600]
Each copy gets its own email_id (suffix -ltNNN), so every order is new. Prints per-order seconds,
throughput, p50/p95 and the dashboard counters at the end. Standard library only.
"""
import base64
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

args = sys.argv[1:]
path = Path(args[0])
count = int(args[args.index("--count") + 1]) if "--count" in args else 20
conc = int(args[args.index("--concurrency") + 1]) if "--concurrency" in args else 5
timeout = int(args[args.index("--timeout") + 1]) if "--timeout" in args else 600
url = "http://localhost:5678/webhook/al-pg-order-notify"
run_tag = time.strftime("%H%M%S")

fx = json.loads(path.read_text(encoding="utf-8"))
atts = []
for a in fx.get("attachments", []):
    data = (path.parent / a["file"]).read_bytes()
    atts.append({"name": Path(a["file"]).name, "mime": a["mime"], "base64": base64.b64encode(data).decode()})


def psql(sql):
    r = subprocess.run(["podman", "exec", "-i", "pg-lab", "psql", "-U", "lab_owner", "-d", "al_poc", "-Atq", "-v", "ON_ERROR_STOP=1"],
                       input=sql, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def dq(s):
    return "$lt$" + s + "$lt$"


ids = [f"{fx['email_id']}-lt{run_tag}-{i:03d}" for i in range(count)]
rows = ", ".join(f"({dq(e)}, {dq(fx['from'])}, {dq(fx['subject'])}, {dq(fx['body'])}, {dq(json.dumps(atts))}::jsonb)" for e in ids)
psql(f"insert into al_poc.mailbox (email_id, from_addr, subject, body_text, attachments) values {rows} on conflict (email_id) do nothing;")

results = {}
lock = threading.Lock()
sem = threading.Semaphore(conc)


def one(email_id):
    with sem:
        t0 = time.time()
        env = {"message": {"data": base64.b64encode(json.dumps({"email_id": email_id}).encode()).decode(), "messageId": f"lt-{email_id}"},
               "subscription": "projects/al-poc-lab/subscriptions/pg-order-intake"}
        req = urllib.request.Request(url, data=json.dumps(env).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:  # noqa: BLE001
            with lock:
                results[email_id] = ("notify_failed", None)
            return
        order_id = "ORD-" + email_id
        while time.time() - t0 < timeout:
            st = psql(f"select status || '|' || coalesce(decision,'') from al_poc.order_intake where order_id = {dq(order_id)};")
            if st.split("|")[0] in ("posted", "pending_review", "rejected", "failed", "not_an_order"):
                with lock:
                    results[email_id] = (st, round(time.time() - t0, 1))
                return
            time.sleep(2)
        with lock:
            results[email_id] = ("timeout", None)


t_start = time.time()
threads = [threading.Thread(target=one, args=(e,)) for e in ids]
for t in threads:
    t.start()
for t in threads:
    t.join()
elapsed = time.time() - t_start

secs = sorted(s for _, s in results.values() if s is not None)
by_status = {}
for st, _ in results.values():
    by_status[st] = by_status.get(st, 0) + 1
print(f"{count} orders, {conc} in flight, {elapsed:.0f} s wall, {count / elapsed * 60:.1f} orders/min")
print("outcomes:", by_status)
if secs:
    p = lambda q: secs[min(len(secs) - 1, int(len(secs) * q))]  # noqa: E731
    print(f"per-order seconds: min {secs[0]}  p50 {p(0.5)}  p95 {p(0.95)}  max {secs[-1]}")
print("dashboard:", psql("select row_to_json(m) from al_poc.metrics m;"))
