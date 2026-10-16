// Compare the workflow JSON in this repository against what is actually
// deployed on an instance. The repo is the source of truth; the canvas is a
// view. This is what verifies that claim instead of asserting it.
//
// Usage: node tools/drift-check.mjs [env]        (default dev)
//        node tools/drift-check.mjs prod --quiet  exit code only, for CI
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { KIT_DIR, resolveTarget, api, apiAll, slugify, normalize, fail } from './lib.mjs';

const args = process.argv.slice(2);
const quiet = args.includes('--quiet');
const envName = args.find((a) => !a.startsWith('--')) ?? 'dev';

const dir = join(KIT_DIR, 'workflows', envName);
if (!existsSync(dir)) fail(`no workflows/${envName}/ directory. Run export-all first.`);

const target = resolveTarget(envName);
const log = (...a) => { if (!quiet) console.log(...a); };

log(`comparing workflows/${envName}/ against ${target.url}\n`);

// --- what the repo says
const repo = new Map();
for (const f of readdirSync(dir)) {
  if (!f.endsWith('.json')) continue;
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(join(dir, f), 'utf8'));
  } catch (e) {
    fail(`workflows/${envName}/${f} is not valid JSON (${e.message})`);
  }
  repo.set(parsed.name ?? f.replace(/\.json$/, ''), { file: f, wf: parsed });
}

// --- what the instance says
const list = await apiAll(target, '/workflows');
const live = new Map();
for (const { id, name } of list) {
  const { status, body } = await api(target, `/workflows/${id}`);
  if (status !== 200 || !body?.name) {
    log(`  WARN: could not read live workflow ${id} (HTTP ${status})`);
    continue;
  }
  live.set(name, body);
}

// --- compare
const onlyInRepo = [];
const onlyOnInstance = [];
const changed = [];
const activeMismatch = [];

for (const [name, { file, wf }] of repo) {
  const l = live.get(name);
  if (!l) { onlyInRepo.push({ name, file }); continue; }

  // Compare only what defines behavior. Ignore ids, timestamps, pin data,
  // and node positions: a node moved on the canvas is not a behavior change.
  const a = normalize(wf);
  const b = normalize(l);
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    changed.push({ name, file, diff: describeDiff(a, b) });
  }
  if ((wf.active ?? false) !== (l.active ?? false)) {
    activeMismatch.push({ name, repo: wf.active ?? false, live: l.active ?? false });
  }
}
for (const name of live.keys()) {
  if (!repo.has(name)) onlyOnInstance.push({ name, file: `${slugify(name)}.json` });
}

/** Human-readable summary of where two normalized workflows differ. */
function describeDiff(a, b) {
  const out = [];
  const an = new Map(a.nodes.map((n) => [n.name, n]));
  const bn = new Map(b.nodes.map((n) => [n.name, n]));
  for (const name of an.keys()) if (!bn.has(name)) out.push(`node only in repo: ${name}`);
  for (const name of bn.keys()) if (!an.has(name)) out.push(`node only on instance: ${name}`);
  for (const [name, node] of an) {
    const other = bn.get(name);
    if (!other) continue;
    if (JSON.stringify(node.parameters) !== JSON.stringify(other.parameters)) {
      out.push(`parameters differ: ${name}`);
    }
    if (node.typeVersion !== other.typeVersion) {
      out.push(`typeVersion differs: ${name} (repo ${node.typeVersion}, instance ${other.typeVersion})`);
    }
    if (JSON.stringify(node.credentials ?? null) !== JSON.stringify(other.credentials ?? null)) {
      out.push(`credentials differ: ${name}`);
    }
  }
  if (JSON.stringify(a.connections) !== JSON.stringify(b.connections)) out.push('connections differ');
  if (JSON.stringify(a.settings) !== JSON.stringify(b.settings)) out.push('settings differ');
  return out.length ? out : ['differs in a field not itemized above'];
}

// --- report
const drifted = onlyInRepo.length + onlyOnInstance.length + changed.length;

if (changed.length) {
  log('CHANGED (repo and instance disagree):');
  for (const c of changed) {
    log(`  ${c.name}`);
    for (const d of c.diff) log(`      ${d}`);
  }
  log();
}
if (onlyOnInstance.length) {
  log('ONLY ON INSTANCE (never exported, so it has no rollback point):');
  for (const o of onlyOnInstance) log(`  ${o.name}`);
  log();
}
if (onlyInRepo.length) {
  log('ONLY IN REPO (deleted or renamed on the instance, or never deployed):');
  for (const o of onlyInRepo) log(`  ${o.name}  (${o.file})`);
  log();
}
if (activeMismatch.length) {
  log('ACTIVE STATE MISMATCH:');
  for (const m of activeMismatch) {
    log(`  ${m.name}: repo says ${m.repo ? 'active' : 'inactive'}, instance says ${m.live ? 'active' : 'inactive'}`);
  }
  log();
}

log(`${repo.size} in repo, ${live.size} on instance, ${drifted} drifted\n`);

// exitCode, not exit(): let the process end on its own so no in-flight socket
// handle is torn down mid-close.
if (drifted === 0 && activeMismatch.length === 0) {
  log('RESULT: in sync');
  process.exitCode = 0;
} else {
  log('RESULT: DRIFT');
  log();
  log('The repository is the source of truth. Resolve deliberately:');
  log('  - instance is right (someone edited the canvas)  -> export-all, review the diff, commit');
  log('  - repo is right (a change never deployed)        -> deploy it, then deactivate/activate');
  log('  - you do not know which                          -> find out before changing anything');
  process.exitCode = 1;
}
