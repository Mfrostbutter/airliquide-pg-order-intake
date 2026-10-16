#!/usr/bin/env python3
"""Environment and target resolution for the Python tools. Standard library only.

Named secrets_env, not secrets, so it never shadows the standard library module.

Lookup order matches lib.mjs: real environment first, then the engagement `.env`,
then an optional secret manager. Running under `infisical run --` needs nothing
here, the values arrive as environment variables. A deployment on a different
secret manager sets N8N_SECRET_CMD to a command that prints one secret to stdout,
with {name} where the secret name goes.

Values are never printed, and never passed on a command line.
"""
from __future__ import annotations

import functools
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Installed into <engagement>/scripts/, so the engagement root is one level up.
KIT_DIR = HERE.parent
LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _env_file() -> Path:
    override = os.environ.get("N8N_ENV_FILE")
    if override:
        return Path(override)
    for p in (KIT_DIR / ".env", HERE / ".env"):
        if p.is_file():
            return p
    return KIT_DIR / ".env"


def _load_env_file():
    """Same parse as lib.mjs loadEnv: strips inline comments and quotes, never overwrites."""
    path = _env_file()
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = LINE.match(line)
        if not m:
            continue
        v = re.sub(r"\s+#.*$", "", m.group(2).strip())
        if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(m.group(1), v)


_load_env_file()


@functools.lru_cache(maxsize=64)
def _from_secret_cmd(name):
    """Fetch one secret with N8N_SECRET_CMD, e.g. 'vault kv get -field={name} secret/n8n'."""
    template = os.environ.get("N8N_SECRET_CMD")
    if not template:
        return None
    args = [a.replace("{name}", name) for a in shlex.split(template)]
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    return r.stdout.strip() or None


def get(name, default=None):
    """Value of `name` from the environment, else the secret command when set, else `default`."""
    v = os.environ.get(name)
    if v:
        return v.strip()
    v = _from_secret_cmd(name)
    if v:
        return v
    return default


def require(name):
    """Same as get(), but exits when the value is missing."""
    v = get(name)
    if not v:
        fail(f"{name} is not set. Put it in {_env_file()}, or run under your secret manager.")
    return v


def resolve_target(env, strict=False):
    """URL and key for an environment. Mirrors resolveTarget in lib.mjs:
    N8N_API_URL_<ENV> / N8N_API_KEY_<ENV>, falling back to the active pair.

    strict=True drops the fallback for a NAMED environment, and every writing
    tool sets it. Falling back is fine for a read; on a write it would push to
    whatever the bare pair points at while the output still says "prod".
    """
    up = (env or "").upper()
    url = get(f"N8N_API_URL_{up}") if up else None
    key = get(f"N8N_API_KEY_{up}") if up else None
    if not (strict and up):
        url = url or get("N8N_API_URL")
        key = key or get("N8N_API_KEY")
    label = env or "active"
    hint = (f'set N8N_API_URL_{up} and N8N_API_KEY_{up}' if strict and up
            else "Copy .env.example to .env and fill it.")
    if not url:
        fail(f'no URL for environment "{label}". {hint}')
    if not key:
        fail(f'no API key for environment "{label}". {hint}')
    url = url.rstrip("/")
    return {"env": env, "url": url, "base": url + "/api/v1", "key": key}


def fail(msg, code=2):
    """Exit like fail() in lib.mjs: ERROR: on stderr, exit 2."""
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)
