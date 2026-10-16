#!/usr/bin/env python3
"""Poll the lab for one order until it finishes and print the record, the step log and the execution status.

Usage: python3 scripts/watch-order.py <order_id> [timeout_seconds]
Reads order_intake and step_log through psql in the pg-lab container; execution status via the lab API if the key is present.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

order_id = sys.argv[1]
timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 300
TERMINAL = {"posted", "rejected", "failed", "not_an_order", "pending_review"}


def psql(sql):
    r = subprocess.run(["podman", "exec", "-i", "pg-lab", "psql", "-U", "lab_owner", "-d", "al_poc", "-Atq", "-F", "\t"],
                       input=sql, capture_output=True, text=True)
    return r.stdout.strip()


t0 = time.time()
status = None
while time.time() - t0 < timeout:
    status = psql(f"select status from al_poc.order_intake where order_id = '{order_id}';")
    if status in TERMINAL:
        break
    time.sleep(5)
print(f"status={status or 'none'} after {int(time.time() - t0)} s")
print(psql(f"select decision, critical_match, confidence, intent, sap_order_number, draft_order_id, execution_id, "
           f"extract(epoch from (coalesce(finished_at, now()) - started_at))::int as seconds from al_poc.order_intake where order_id = '{order_id}';"))
print("--- steps")
print(psql(f"select to_char(at, 'HH24:MI:SS'), agent, step, left(message, 140) from al_poc.step_log where order_id = '{order_id}' order by id;"))
print("--- rules")
print(psql(f"select r->>'id', r->>'pass', left(r->>'detail', 120) from al_poc.order_intake o, jsonb_array_elements(o.rules) r where o.order_id = '{order_id}';"))
key = os.environ.get("N8N_LOCAL_WORKFLOW_LAB_API_KEY")
exec_id = psql(f"select execution_id from al_poc.order_intake where order_id = '{order_id}';")
if key and exec_id:
    req = urllib.request.Request(f"http://localhost:5678/api/v1/executions/{exec_id}", headers={"X-N8N-API-KEY": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            e = json.load(r)
            print(f"--- execution {exec_id}: status={e.get('status')} finished={e.get('finished')} started={e.get('startedAt')} stopped={e.get('stoppedAt')}")
    except Exception as ex:
        print("execution lookup failed:", ex)
