#!/usr/bin/env python3
"""Load an email fixture into the lab mailbox (Gmail stand-in) and send the Pub/Sub-style notification.

Usage: python3 scripts/send-fixture.py data/fixtures/<name>.json [--chaos N] [--test]
  --chaos N   make the mock ESB fail the first N posts for this order (exponential backoff demo)
  --test      hit the /webhook-test/ URL instead of the production webhook
  --no-purge  keep the previous run of this order (rehearses a Pub/Sub redelivery: the intake must drop it)
A fixture may list attachments as {"file": "<relative path>", "mime": "..."}; files are base64-encoded on the way in.
Standard library only; the mailbox row is written through psql in the pg-lab container.
"""
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

path = Path(sys.argv[1])
chaos = int(sys.argv[sys.argv.index("--chaos") + 1]) if "--chaos" in sys.argv else 0
url = "http://localhost:5678/webhook/al-pg-order-notify"
if "--test" in sys.argv:
    url = url.replace("/webhook/", "/webhook-test/")

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
    return "$fx$" + s + "$fx$"


order_id = "ORD-" + fx["email_id"]
if "--no-purge" not in sys.argv:
    # A re-sent fixture is a fresh order. Without --no-purge the previous run of this order is removed first.
    psql(f"delete from al_poc.step_log where order_id = {dq(order_id)}; delete from al_poc.draft_orders where order_id = {dq(order_id)}; "
         f"delete from al_poc.sales_orders where order_id = {dq(order_id)}; delete from al_poc.order_intake where order_id = {dq(order_id)};")
psql(
    "insert into al_poc.mailbox (email_id, from_addr, subject, body_text, attachments) values ("
    f"{dq(fx['email_id'])}, {dq(fx['from'])}, {dq(fx['subject'])}, {dq(fx['body'])}, {dq(json.dumps(atts))}::jsonb) "
    "on conflict (email_id) do update set from_addr = excluded.from_addr, subject = excluded.subject, "
    "body_text = excluded.body_text, attachments = excluded.attachments, received_at = now();"
)
psql(f"delete from al_poc.esb_chaos where order_id = {dq(order_id)};")
if chaos:
    psql(f"insert into al_poc.esb_chaos (order_id, remaining_failures) values ({dq(order_id)}, {chaos});")

# Pub/Sub push envelope: the notification carries only the pointer, the payload is pulled afterwards.
envelope = {"message": {"data": base64.b64encode(json.dumps({"email_id": fx["email_id"]}).encode()).decode(),
                        "messageId": f"lab-{fx['email_id']}", "attributes": {"source": "gmail-watch"}},
            "subscription": "projects/al-poc-lab/subscriptions/pg-order-intake"}
req = urllib.request.Request(url, data=json.dumps(envelope).encode(), method="POST",
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        print(r.status, r.read().decode()[:300])
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()[:500])
print(f"order {order_id} notified; chaos={chaos}")
