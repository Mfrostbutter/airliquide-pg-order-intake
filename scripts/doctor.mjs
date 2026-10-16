// Verifies a working directory is set up correctly: environment, instance
// reachability, API capability, and repo state. Run it when something feels
// wrong before assuming a workflow is broken.
//
// Usage: node tools/doctor.mjs [env]
import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { KIT_DIR, loadEnv, resolveTarget, api } from './lib.mjs';

const envName = process.argv[2] ?? 'dev';
let problems = 0;
const ok = (m) => console.log(`  ok    ${m}`);
const bad = (m, fix) => { console.log(`  FAIL  ${m}`); if (fix) console.log(`        -> ${fix}`); problems += 1; };
const note = (m) => console.log(`  note  ${m}`);

await main();

// exitCode, not exit(): let the process end on its own so no in-flight socket
// handle is torn down mid-close.
console.log();
console.log(problems === 0 ? 'RESULT: ok' : `RESULT: ${problems} problem${problems === 1 ? '' : 's'} above`);
process.exitCode = problems ? 1 : 0;

async function main() {
  console.log(`doctor: ${KIT_DIR}\n`);

  // --- 1. Runtime
  console.log('runtime');
  const major = Number(process.versions.node.split('.')[0]);
  if (major >= 18) ok(`node ${process.versions.node}`);
  else bad(`node ${process.versions.node} is too old`, 'these tools need Node 18+ (fetch, top-level await)');

  // --- 2. Environment
  console.log('\nenvironment');
  loadEnv();
  if (existsSync(join(KIT_DIR, '.env'))) ok('.env present');
  else bad('.env missing', 'cp .env.example .env, then fill it');

  const up = envName.toUpperCase();
  const url = process.env[`N8N_API_URL_${up}`] ?? process.env.N8N_API_URL;
  const key = process.env[`N8N_API_KEY_${up}`] ?? process.env.N8N_API_KEY;
  if (url) ok(`URL for "${envName}": ${url}`);
  else bad(`no URL for "${envName}"`, `set N8N_API_URL_${up} or N8N_API_URL`);
  if (key) ok(`API key for "${envName}": present (${key.length} chars)`);
  else bad(`no API key for "${envName}"`, `set N8N_API_KEY_${up} or N8N_API_KEY`);

  // A prod key in the active slot is a footgun: every default-target command
  // would hit production.
  if (process.env.N8N_API_URL && process.env.N8N_API_URL_PROD &&
      process.env.N8N_API_URL === process.env.N8N_API_URL_PROD) {
    note('the ACTIVE target is production. Every default command writes there. Intentional?');
  }

  // Nothing below can run without a target. Stop here rather than reporting
  // misleading failures about an instance we were never told how to reach.
  if (!url || !key) return;

  // --- 3. Instance
  console.log('\ninstance');
  const target = resolveTarget(envName);
  const probe = await api(target, '/workflows?limit=1', { timeoutMs: 15000 });
  if (probe.status === 200) {
    ok('reachable and authenticated');
  } else if (probe.status === 0) {
    bad(`unreachable (${probe.error ?? 'no response'})`, 'network, DNS, VPN, or the instance is down. Infrastructure, not a workflow.');
  } else if (probe.status === 401 || probe.status === 403) {
    bad(`API key rejected (${probe.status})`, 'key expired or revoked, or the instance encryption key changed');
  } else if (probe.status === 404) {
    bad('public API returned 404', 'the public API may be disabled on this instance');
  } else {
    bad(`unexpected HTTP ${probe.status}`);
  }

  if (probe.status === 200) {
    // Capability probe: several operations need endpoints that are not always
    // present or permitted.
    for (const [label, path] of [
      ['executions', '/executions?limit=1'],
      ['credential schema', '/credentials/schema/httpBasicAuth'],
    ]) {
      const r = await api(target, path, { timeoutMs: 10000 });
      if (r.status === 200) ok(`${label} readable`);
      else note(`${label} returned HTTP ${r.status} (some operations will be unavailable)`);
    }
  }

  // --- 4. Repo state
  console.log('\nrepo');
  const wfDir = join(KIT_DIR, 'workflows', envName);
  if (existsSync(wfDir)) {
    const n = readdirSync(wfDir).filter((f) => f.endsWith('.json')).length;
    if (n > 0) ok(`workflows/${envName}/ has ${n} exported workflow${n === 1 ? '' : 's'}`);
    else note(`workflows/${envName}/ is empty. Run export-all to create a rollback point.`);
  } else {
    note(`no workflows/${envName}/ directory yet. Run export-all.`);
  }

  if (existsSync(join(KIT_DIR, '.git'))) ok('git repository present (rollback is possible)');
  else bad('not a git repository', 'git init. Without git there is no undo for any change.');

  if (existsSync(join(KIT_DIR, '.gitignore'))) ok('.gitignore present');
  else bad('.gitignore missing', '.env would be committable. Add one before anything else.');
}
