// Snapshot every workflow from an instance into workflows/<env>/. The undo point.
// Run and commit before any change. No AI required.
// Usage: node scripts/export-all.mjs [env]   (default dev)
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { KIT_DIR, resolveTarget, api, apiAll, slugify, sortKeys, fail } from './lib.mjs';

const envName = process.argv[2] ?? 'dev';
const target = resolveTarget(envName);
const outDir = join(KIT_DIR, 'workflows', envName === 'staging' ? 'dev' : envName);

console.log(`exporting from ${target.url} -> workflows/${envName}/`);
mkdirSync(outDir, { recursive: true });

// The list endpoint omits nodes and connections, so fetch each workflow by id.
const list = await apiAll(target, '/workflows');
if (!list.length) fail('no workflows returned. Check the URL, the key, and that the public API is enabled.', 1);

let count = 0;
const seen = new Map();
for (const { id } of list) {
  const { status, body } = await api(target, `/workflows/${id}`);
  if (status !== 200 || !body?.name) {
    console.log(`  skipped ${id} (HTTP ${status})`);
    continue;
  }

  // Two workflows can share a name; keep both rather than silently overwriting.
  let slug = slugify(body.name);
  const n = (seen.get(slug) ?? 0) + 1;
  seen.set(slug, n);
  if (n > 1) slug = `${slug}-${id}`;

  const snapshot = sortKeys({
    name: body.name,
    nodes: body.nodes ?? [],
    connections: body.connections ?? {},
    settings: body.settings ?? {},
    active: body.active ?? false,
    tags: (body.tags ?? []).map((t) => t.name ?? t),
  });

  writeFileSync(join(outDir, `${slug}.json`), `${JSON.stringify(snapshot, null, 2)}\n`, 'utf8');
  console.log(`  ${slug}.json`);
  count += 1;
}

console.log(`\nexported ${count} workflows\n`);
console.log('Now commit. This is the rollback point:');
console.log(`  git add -A && git commit -m 'snapshot ${envName} before <change>'\n`);
console.log('NOTE: workflow JSON embeds credential REFERENCES and internal hostnames.');
console.log('It does not contain credential values. Review a diff before pushing anywhere public.');
