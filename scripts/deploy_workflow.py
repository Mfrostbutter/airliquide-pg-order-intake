#!/usr/bin/env python3
"""Deploy a workflow JSON to an engagement instance. Standard library only.

  deploy.sh <workflow.json> [env] [--activate] [--confirm] [--dry-run]

Defaults to dev. Staging and prod are named targets and need --confirm, because
a promotion is a deliberate act and an ad-hoc write to production is not one.

Two HARD gates run before the server is touched, and abort on failure:
  tag gate     the JSON carries "tags": [...] so the instance stays sortable
  canvas gate  n8n-canvas-docs/scripts/layout_check.py prints RESULT: ok

After the write it read-backs and verifies the node set and the publish state.
Credential ids are instance-local, so they come from deploy/creds.<env>.json by
NAME; an unknown name has its credential block stripped rather than pushed with
a dangling id from another instance.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import secrets_env
from secrets_env import KIT_DIR, fail

SETTINGS_KEYS = ("executionOrder", "timezone", "saveDataErrorExecution",
                 "saveDataSuccessExecution", "saveManualExecutions",
                 "saveExecutionProgress", "errorWorkflow", "executionTimeout")
LEDGER_DIR = KIT_DIR / "deploy"


def api(target, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(target["base"] + path, data=data, method=method)
    req.add_header("X-N8N-API-KEY", target["key"])
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "n8n-care-kit")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode()
            return r.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:500]}
    except OSError as e:
        return 0, {"error": str(e)}


def ledger(kind, env):
    """deploy/<kind>.<env>.json. Credential ids are instance-local, so per env."""
    p = LEDGER_DIR / f"{kind}.{env}.json"
    return p, (json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {})


def layout_gate(src):
    """HARD gate: refuse to deploy an undocumented canvas. Runs the n8n-canvas-docs
    checker; no `RESULT: ok`, no deploy. Override with N8N_LAYOUT_CHECK."""
    rel = os.path.join("n8n-canvas-docs", "scripts", "layout_check.py")
    candidates = [os.environ.get("N8N_LAYOUT_CHECK"),
                  str(KIT_DIR / ".claude" / "skills" / rel),
                  os.path.join(os.path.expanduser("~"), ".claude", "skills", rel)]
    checker = next((c for c in candidates if c and os.path.exists(c)), None)
    if not checker:
        fail("canvas gate: layout_check.py not found. Install the n8n-canvas-docs "
             "skill into .claude/skills/, or set N8N_LAYOUT_CHECK.")
    r = subprocess.run([sys.executable, checker, src], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(r.stdout.strip())
    if r.returncode != 0 or "RESULT: ok" not in r.stdout:
        fail("canvas gate FAILED. Document the canvas (zone spec + "
             "n8n-canvas-docs/scripts/zone_layout.py) until layout_check prints RESULT: ok.", 1)


def ensure_tags(target, names):
    """Resolve tag names to ids on the target, creating any that are missing."""
    code, resp = api(target, "GET", "/tags?limit=250")
    existing = {t["name"]: t["id"] for t in resp.get("data", [])} if code == 200 else {}
    ids = []
    for name in names:
        tid = existing.get(name)
        if not tid:
            c, r = api(target, "POST", "/tags", {"name": name})
            if c not in (200, 201):
                fail(f"tag create FAILED for '{name}' HTTP {c}: {r.get('error')}", 1)
            tid = r["id"]
        ids.append(tid)
    return ids


def wire_credentials(wf, creds, wf_ids):
    """Ledger id wins; otherwise the id shipped in the JSON stands. A name with
    neither is stripped: a dangling id from another instance binds nothing and
    hides the problem until activation."""
    for node in wf["nodes"]:
        block = node.get("credentials")
        if block:
            for ctype, ref in list(block.items()):
                cid = creds.get(ref.get("name", "")) or ref.get("id")
                if cid:
                    block[ctype] = {"id": cid, "name": ref["name"]}
                else:
                    print(f"  no credential id for '{ref.get('name')}' on node '{node['name']}', block stripped")
                    del block[ctype]
            if not block:
                del node["credentials"]
        # Execute Sub-workflow nodes carry a pinned id plus __workflowName.
        p = node.get("parameters", {})
        ref = p.get("workflowId", {})
        if isinstance(ref, dict) and ref.get("__workflowName"):
            sub = ref["__workflowName"]
            wid = wf_ids.get(sub) or ref.get("value")
            if not wid:
                fail(f"sub-workflow '{sub}' is not deployed to this environment yet", 1)
            p["workflowId"] = {"__rl": True, "mode": "id", "value": wid}
    return wf


def main():
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    pos = [a for a in args if not a.startswith("--")]
    unknown = flags - {"--activate", "--confirm", "--dry-run"}
    if unknown or not pos:
        fail(f"usage: deploy.sh <workflow.json> [env] [--activate] [--confirm] [--dry-run]"
             + (f"\n  unknown flag(s): {', '.join(sorted(unknown))}" if unknown else ""))
    src = pos[0]
    env = pos[1] if len(pos) > 1 else "dev"
    activate, dry = "--activate" in flags, "--dry-run" in flags

    # Rule 0: dev is the default, everything else is a named target you confirm.
    if env != "dev" and "--confirm" not in flags:
        fail(f'"{env}" is not dev. Promotions are deliberate: re-run with --confirm.')

    if not os.path.exists(src):
        fail(f"no such file: {src}")
    wf = json.load(open(src, encoding="utf-8"))

    # HARD gates, before any request, so a failure leaves the instance untouched.
    tags = wf.get("tags") or []
    if not tags:
        fail('tag gate: workflow has no tags. Add "tags": [...] to the JSON '
             "(SOP: every workflow is tagged).", 1)
    layout_gate(src)

    # strict: a named env must have its own pair, never the bare fallback.
    target = secrets_env.resolve_target(env, strict=True)
    cred_path, creds = ledger("creds", env)
    wf_path, wf_ids = ledger("workflows", env)

    wf = wire_credentials(wf, creds, wf_ids)
    body = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": {k: v for k, v in wf.get("settings", {}).items() if k in SETTINGS_KEYS},
    }
    # Ledger first, then the id pinned in the JSON by an earlier import.
    wid = wf_ids.get(wf["name"]) or wf.get("id")

    print(f"\n{wf['name']}\n  target  {target['url']}  ({env})"
          f"\n  nodes   {len(body['nodes'])}"
          f"\n  action  {'update ' + wid if wid else 'create'}{'  + activate' if activate else ''}")
    if dry:
        print("\nDry run. Gates passed, nothing written.")
        return

    # Activate BEFORE the PUT. POST /activate republishes whatever version was
    # already active, which silently discards a PUT made just before it: a node
    # edited in that PUT can drop out of the running workflow with no error, and
    # the post-PUT read-back still looks clean because it returns the draft.
    if activate and wid:
        code, _ = api(target, "POST", f"/workflows/{wid}/activate")
        print(f"pre-activate -> HTTP {code}")

    code, resp, verb = 404, {}, "updated"
    if wid:
        code, resp = api(target, "PUT", f"/workflows/{wid}", body)
    if code == 404:   # id is not on this instance yet
        code, resp = api(target, "POST", "/workflows", body)
        verb = "created"
    if code not in (200, 201):
        fail(f"{verb} FAILED HTTP {code}: {resp.get('error')}", 1)
    wid = resp["id"]
    print(f"{verb} -> {wid}")

    if activate:
        code, _ = api(target, "POST", f"/workflows/{wid}/activate")
        print(f"activate -> HTTP {code}")

    # Verify at runtime. 200 is not proof the change took effect.
    code, live = api(target, "GET", f"/workflows/{wid}")
    if code != 200:
        fail(f"VERIFY FAILED: read-back returned HTTP {code}", 1)
    want = {n["name"] for n in body["nodes"]}
    got = {n["name"] for n in live.get("nodes", [])}
    if want != got:
        fail(f"VERIFY FAILED: missing on server {sorted(want - got)}, extra {sorted(got - want)}", 1)
    print(f"verified {len(got)} nodes on server")

    # A draft that never got published is a deploy that never landed. An inactive
    # workflow has no active version yet, so the check only applies once it is active.
    if live.get("active") and live.get("versionId") != live.get("activeVersionId"):
        fail("VERIFY FAILED: an unpublished draft is live "
             f"(versionId {live.get('versionId')} != activeVersionId {live.get('activeVersionId')}). "
             "Re-run the deploy, or press Publish in the editor.", 1)
    print("published (versionId == activeVersionId)" if live.get("active")
          else "inactive; publish state is checked on activation")

    ids = ensure_tags(target, tags)
    code, resp = api(target, "PUT", f"/workflows/{wid}/tags", [{"id": i} for i in ids])
    if code not in (200, 201):
        fail(f"tag assign FAILED HTTP {code}: {resp.get('error')}", 1)
    print(f"tagged -> {tags}")

    # Record the id last, so the ledger only ever names a verified deploy.
    if wf_ids.get(wf["name"]) != wid:
        wf_ids[wf["name"]] = wid
        LEDGER_DIR.mkdir(exist_ok=True)
        wf_path.write_text(json.dumps(wf_ids, indent=2) + "\n", encoding="utf-8")
        print(f"ledger  {wf_path.relative_to(KIT_DIR)}")
    if not creds:
        print(f"note    no {cred_path.relative_to(KIT_DIR)}; credentials bound by the ids in the JSON only")


if __name__ == "__main__":
    main()
