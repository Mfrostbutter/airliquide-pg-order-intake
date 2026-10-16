// Shared helpers for the care-kit scripts. Node built-ins only, no packages.
import { readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const KIT_DIR = dirname(dirname(fileURLToPath(import.meta.url)));

/** Loads .env into process.env without overwriting existing values. */
export function loadEnv() {
  const f = join(KIT_DIR, '.env');
  if (!existsSync(f)) return;
  for (const line of readFileSync(f, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) continue;
    let v = m[2].trim().replace(/\s+#.*$/, '');
    if (/^".*"$|^'.*'$/.test(v)) v = v.slice(1, -1);
    if (process.env[m[1]] === undefined) process.env[m[1]] = v;
  }
}

/** Resolves the URL and key for an environment, falling back to the active pair. */
export function resolveTarget(env) {
  loadEnv();
  const up = (env || '').toUpperCase();
  const url = (up && process.env[`N8N_API_URL_${up}`]) || process.env.N8N_API_URL;
  const key = (up && process.env[`N8N_API_KEY_${up}`]) || process.env.N8N_API_KEY;
  if (!url) fail(`no URL for environment "${env || 'active'}". Copy .env.example to .env and fill it.`);
  if (!key) fail(`no API key for environment "${env || 'active'}". Copy .env.example to .env and fill it.`);
  return { base: `${url.replace(/\/+$/, '')}/api/v1`, url: url.replace(/\/+$/, ''), key };
}

/** GET against the n8n public API. Returns {status, body}. Never throws on HTTP status. */
export async function api(target, path, { timeoutMs = 30000 } = {}) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(`${target.base}${path}`, {
      headers: {
        'X-N8N-API-KEY': target.key,
        Accept: 'application/json',
        // Some CDNs bot-flag a missing user agent independently of any rule.
        'User-Agent': 'n8n-care-kit',
        // Do not leave keep-alive sockets open. These are short-lived CLI runs,
        // and a lingering socket keeps libuv handles alive at exit (on Windows
        // that surfaces as a fast-fail crash instead of a clean exit code).
        Connection: 'close',
      },
      signal: ctl.signal,
    });
    let body = null;
    try { body = await res.json(); } catch { /* non-JSON error page */ }
    return { status: res.status, body };
  } catch (e) {
    return { status: 0, body: null, error: e.message };
  } finally {
    clearTimeout(t);
  }
}

/** Pages through a list endpoint via the cursor. */
export async function apiAll(target, path) {
  const out = [];
  let cursor = null;
  do {
    const q = `${path}${path.includes('?') ? '&' : '?'}limit=250${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
    const { status, body, error } = await api(target, q);
    if (status !== 200) fail(`GET ${q} returned ${status}${error ? ` (${error})` : ''}`);
    out.push(...(body?.data ?? []));
    cursor = body?.nextCursor ?? null;
  } while (cursor);
  return out;
}

/** Recursively sorts object keys so a serialized diff shows real changes. */
export function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === 'object') {
    return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])]));
  }
  return v;
}

// Fields that define what a workflow DOES. Everything else (ids, timestamps,
// canvas positions, pinned test data, instance-assigned webhook ids) is noise
// for a comparison: a node dragged across the canvas is not a behavior change.
const BEHAVIOR_FIELDS = [
  'name', 'type', 'typeVersion', 'parameters', 'credentials',
  'disabled', 'onError', 'retryOnFail', 'maxTries', 'waitBetweenTries',
  'alwaysOutputData', 'executeOnce', 'notesInFlow', 'notes',
];

/** Reduces a workflow to its behavior, for comparison only. Never for export. */
export function normalize(wf) {
  const nodes = (wf.nodes ?? [])
    .filter((n) => !String(n.type ?? '').endsWith('stickyNote'))
    .map((n) => {
      const out = {};
      for (const f of BEHAVIOR_FIELDS) if (n[f] !== undefined) out[f] = n[f];
      return out;
    })
    .sort((a, b) => String(a.name).localeCompare(String(b.name)));

  return sortKeys({
    name: wf.name,
    nodes,
    connections: wf.connections ?? {},
    settings: wf.settings ?? {},
  });
}

export function fail(msg, code = 2) {
  console.error(`ERROR: ${msg}`);
  process.exit(code);
}

/** Filename-safe slug from a workflow name. */
export function slugify(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'workflow';
}
