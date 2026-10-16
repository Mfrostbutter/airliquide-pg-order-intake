#!/usr/bin/env python3
"""One-time lab setup for the Air Liquide PG Order Intake POC: Postgres database, app role, n8n credential.

Usage: infisical run --projectId <id> --env dev --silent -- python3 scripts/lab-setup-db.py
Idempotent. The generated role password goes to the n8n credential and to Infisical
(PG_LAB_AL_POC_PASSWORD, env dev). It is never printed.
"""
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("N8N_LAB_URL", "http://localhost:5678").rstrip("/")
KEY = os.environ.get("N8N_LOCAL_WORKFLOW_LAB_API_KEY")
PROJECT = "7fe3abe3-07d8-48cf-af75-14c038f94174"
DB, ROLE, CRED_NAME = "al_poc", "al_poc_app", "Lab Postgres al_poc"
if not KEY:
    sys.exit("N8N_LOCAL_WORKFLOW_LAB_API_KEY not set")


def psql(db, sql):
    r = subprocess.run(["podman", "exec", "-i", "pg-lab", "psql", "-U", "lab_owner", "-d", db, "-Atq", "-v", "ON_ERROR_STOP=1"],
                       input=sql, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}/api/v1{path}", data=data, method=method)
    req.add_header("X-N8N-API-KEY", KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}


def main():
    existing = {c["name"]: c["id"] for c in api("GET", "/credentials?limit=250")[1].get("data", [])}
    if CRED_NAME in existing:
        print(json.dumps({"credential": CRED_NAME, "id": existing[CRED_NAME], "action": "exists"}))
        return

    pw = secrets.token_urlsafe(24)
    role_exists = psql("postgres", f"select 1 from pg_roles where rolname='{ROLE}';") == "1"
    if role_exists:
        psql("postgres", f"alter role {ROLE} with login password '{pw}';")
    else:
        psql("postgres", f"create role {ROLE} with login password '{pw}';")
    if psql("postgres", f"select 1 from pg_database where datname='{DB}';") != "1":
        psql("postgres", f"create database {DB} owner {ROLE};")
    print(json.dumps({"database": DB, "role": ROLE, "role_existed": role_exists}))

    r = subprocess.run(["infisical", "secrets", "set", f"PG_LAB_AL_POC_PASSWORD={pw}",
                        "--projectId", PROJECT, "--env", "dev", "--silent"],
                       capture_output=True, text=True)
    print(json.dumps({"infisical": "ok" if r.returncode == 0 else f"failed rc={r.returncode}"}))

    status, resp = api("POST", "/credentials", {
        "name": CRED_NAME,
        "type": "postgres",
        "data": {"host": "pg-lab", "port": 5432, "database": DB, "user": ROLE,
                 "password": pw, "ssl": "disable", "allowUnauthorizedCerts": False},
    })
    if status not in (200, 201):
        sys.exit(f"credential create failed: {status} {resp}")
    print(json.dumps({"credential": CRED_NAME, "id": resp.get("id"), "action": "created"}))


if __name__ == "__main__":
    main()
