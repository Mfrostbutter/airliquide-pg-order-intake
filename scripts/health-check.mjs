// Instance reachable, workflows active, recent failure count. No AI required.
// Usage: node scripts/health-check.mjs [hours] [--env dev]
import { resolveTarget, api, apiAll } from './lib.mjs';

const args = process.argv.slice(2);
const envIdx = args.indexOf('--env');
const envName = envIdx >= 0 ? args[envIdx + 1] : '';
const hours = Number(args.find((a) => /^\d+$/.test(a)) ?? 24);

const target = resolveTarget(envName);
console.log(`instance: ${target.url}`);
console.log(`window:   last ${hours}h\n`);

// 1. Reachable and authenticated.
const probe = await api(target, '/workflows?limit=1', { timeoutMs: 15000 });
if (probe.status === 0) {
  console.log('reachable:      NO (no response)');
  console.log('                -> network, DNS, VPN, or the instance is down.');
  console.log('                   Infrastructure, not a workflow. Escalate per HANDOFF.md.');
  process.exit(1);
}
if (probe.status === 401 || probe.status === 403) {
  console.log(`reachable:      yes, but the API key was rejected (${probe.status})`);
  console.log('                -> key expired or revoked, or the instance encryption key changed.');
  console.log('                   An instance-wide credential failure points at the encryption key.');
  process.exit(1);
}
if (probe.status !== 200) {
  console.log(`reachable:      unexpected HTTP ${probe.status}`);
  process.exit(1);
}
console.log('reachable:      yes');

// 2. Workflow inventory.
const workflows = await apiAll(target, '/workflows');
const active = workflows.filter((w) => w.active);
console.log(`workflows:      ${workflows.length} total, ${active.length} active`);

// 3. Recent failures.
const since = new Date(Date.now() - hours * 3600_000).toISOString();
const errored = (await apiAll(target, '/executions?status=error'))
  .filter((e) => (e.startedAt ?? '') >= since);
console.log(`failures:       ${errored.length} in the last ${hours}h`);

if (errored.length) {
  const byWorkflow = new Map();
  for (const e of errored) {
    const name = e.workflowData?.name ?? e.workflowName ?? e.workflowId ?? 'unknown';
    byWorkflow.set(name, (byWorkflow.get(name) ?? 0) + 1);
  }
  console.log('\nfailing workflows:');
  for (const [name, n] of [...byWorkflow].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${String(n).padStart(3)}x  ${name}`);
  }
}

// 4. Inactive workflows are the most common "it stopped working".
const inactive = workflows.filter((w) => !w.active);
if (inactive.length) {
  console.log('\ninactive workflows (expected? check WORKFLOWS.md):');
  for (const w of inactive) console.log(`  ${w.name}`);
}

console.log();
if (errored.length === 0) {
  console.log('RESULT: healthy');
} else {
  console.log(`RESULT: ${errored.length} failures -> RUNBOOKS/incident-workflow-failing.md`);
}
