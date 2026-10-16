#!/usr/bin/env python3
"""Call the local lab n8n public API. Standard library only.

Usage: infisical run --projectId <id> --env dev --silent -- python3 scripts/lab-api.py METHOD PATH [json-file]
Reads N8N_LOCAL_WORKFLOW_LAB_API_KEY from the environment. Prints the response body.
Credential 'data' fields are never echoed.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("N8N_LAB_URL", "http://localhost:5678").rstrip("/")
KEY = os.environ.get("N8N_LOCAL_WORKFLOW_LAB_API_KEY")
if not KEY:
    sys.exit("N8N_LOCAL_WORKFLOW_LAB_API_KEY not set")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}/api/v1{path}", data=data, method=method)
    req.add_header("X-N8N-API-KEY", KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:800]}


def scrub(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k == "data" and "type" in obj else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--out=")]
    out = next((a[6:] for a in sys.argv[1:] if a.startswith("--out=")), None)
    method, path = args[0], args[1]
    body = json.load(open(args[2])) if len(args) > 2 else None
    status, resp = call(method, path, body)
    text = json.dumps(scrub(resp), indent=1)
    print(status)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {len(text)} chars to {out}")
    else:
        print(text[:6000])


if __name__ == "__main__":
    main()
