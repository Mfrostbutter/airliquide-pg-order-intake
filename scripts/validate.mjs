// Structural validation of a workflow JSON. Headless, no AI, no network.
// Usage: node scripts/validate.mjs workflows/dev/my-workflow.json
import { readFileSync, existsSync } from 'node:fs';

const file = process.argv[2];
if (!file) { console.error('usage: validate.mjs <workflow.json>'); process.exit(2); }
if (!existsSync(file)) { console.error(`no such file: ${file}`); process.exit(2); }

const errors = [];
const warnings = [];
const err = (m) => errors.push(m);
const warn = (m) => warnings.push(m);

console.log(`validating ${file}\n`);

let wf;
try {
  wf = JSON.parse(readFileSync(file, 'utf8'));
} catch (e) {
  console.log(`ERROR: not valid JSON (${e.message})\n\nRESULT: BLOCKED`);
  process.exit(1);
}

for (const k of ['name', 'nodes', 'connections']) {
  if (!(k in wf)) err(`missing top-level key: ${k}`);
}

const nodes = Array.isArray(wf.nodes) ? wf.nodes : [];
console.log(`nodes: ${nodes.length}`);
if (!nodes.length) err('workflow has no nodes');

// Every node needs name, type, typeVersion.
for (const n of nodes) {
  const label = n.name || '<unnamed>';
  if (!n.name) err(`node missing name (type ${n.type ?? '?'})`);
  if (!n.type) err(`node missing type: ${label}`);
  if (n.typeVersion === undefined || n.typeVersion === null) err(`node missing typeVersion: ${label}`);
}

// Node names must be unique: connections are keyed by name, so duplicates corrupt wiring.
const counts = new Map();
for (const n of nodes) counts.set(n.name, (counts.get(n.name) ?? 0) + 1);
for (const [name, c] of counts) if (c > 1) err(`duplicate node name: ${name}`);

// Every connection must reference a node that exists.
const names = new Set(nodes.map((n) => n.name));
const conns = wf.connections ?? {};
const targets = new Set();
for (const [source, outputs] of Object.entries(conns)) {
  if (!names.has(source)) err(`connection source does not exist: ${source}`);
  for (const branches of Object.values(outputs ?? {})) {
    for (const branch of branches ?? []) {
      for (const link of branch ?? []) {
        if (!link?.node) continue;
        targets.add(link.node);
        if (!names.has(link.node)) err(`connection target does not exist: ${link.node}`);
      }
    }
  }
}

// A trigger must exist or nothing can start the workflow.
const isTrigger = (n) => /trigger$/i.test(n.type ?? '') || /\.webhook$/i.test(n.type ?? '');
if (!nodes.some(isTrigger)) err('no trigger node: this workflow cannot start');

// Disconnected nodes are legal but usually an editing mistake.
for (const n of nodes) {
  if (isTrigger(n) || (n.type ?? '').endsWith('stickyNote')) continue;
  if (!conns[n.name] && !targets.has(n.name)) warn(`node is not connected to anything: ${n.name}`);
}

// Placeholder credential ids permanently disable the credential selector in the n8n UI.
for (const n of nodes) {
  for (const c of Object.values(n.credentials ?? {})) {
    if (/REPLACE|TODO|XXX|CHANGE_?ME|placeholder/i.test(c?.id ?? '')) {
      err(`placeholder credential id on node: ${n.name} (remove the credentials block instead)`);
    }
  }
}

// Hardcoded secrets. Credentials belong in the credential store, never a parameter.
const SECRET = /(api[_-]?key|secret|password|passwd|token|authorization)"?\s*[:=]\s*"(?!=\{\{)[A-Za-z0-9_\-./+]{16,}"/i;
const TOKEN_SHAPE = /\b(sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})\b/;
for (const n of nodes) {
  const s = JSON.stringify(n.parameters ?? {});
  if (SECRET.test(s) || TOKEN_SHAPE.test(s)) {
    err(`looks like a hardcoded secret in parameters on node: ${n.name}`);
  }
}

// A webhook answering through a Respond node must have an error path or it hangs.
for (const n of nodes) {
  if (!/webhook/i.test(n.type ?? '')) continue;
  if (n.parameters?.responseMode !== 'responseNode') continue;
  if (n.onError !== 'continueRegularOutput') {
    warn(`webhook "${n.name}" responds via a Respond node but has no error path (set onError: continueRegularOutput)`);
  }
}

// Object literals inside {{ }} break n8n's expression extender, non-deterministically.
for (const n of nodes) {
  for (const [k, v] of Object.entries(n.parameters ?? {})) {
    if (typeof v !== 'string' || !v.startsWith('=')) continue;
    if (/\{\{[^}]*\{\s*[A-Za-z_'"]/.test(v)) {
      warn(`node "${n.name}" parameter "${k}" has an object literal inside {{ }}: build it in a Code node and reference the result`);
    }
  }
}

for (const w of warnings) console.log(`WARN: ${w}`);
for (const e of errors) console.log(`ERROR: ${e}`);

console.log();
if (errors.length === 0) {
  console.log('RESULT: ok\n');
  console.log('Structural checks passed. That means the JSON is well-formed, NOT that the');
  console.log('workflow is correct. Still run it on dev and look at the result.');
  process.exit(0);
} else {
  console.log('RESULT: BLOCKED');
  process.exit(1);
}
