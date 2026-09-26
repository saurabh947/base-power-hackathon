// Fails the build if anything from web/mock/ reached the production bundle.
// Runs after `vite build` (see the "build" script in package.json).

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const DIST = new URL('../dist/', import.meta.url).pathname;
const FORBIDDEN = [
  'FLEET_SIM_MOCK_DO_NOT_SHIP', // MOCK_MARKER in mock/sim.ts
  'mock (simulated, not TimesFM)', // the mock's forecast model name
  'Exported from data/snapshot/ercot_rt_2025_hubs.parquet', // the mock's price fixture
  'The practice server', // mock error messages
];

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else yield path;
  }
}

let problems = 0;
let checked = 0;
for (const file of files(DIST)) {
  if (!/\.(js|mjs|css|html|json|map)$/.test(file)) continue;
  checked++;
  const text = readFileSync(file, 'utf8');
  for (const needle of FORBIDDEN) {
    // Source maps list source paths; only their embedded sources matter, which the text search covers.
    if (text.includes(needle)) {
      console.error(`check-dist: "${needle}" found in ${file}: the mock must not ship.`);
      problems++;
    }
  }
}

if (problems) process.exit(1);
console.info(`check-dist: ${checked} files checked, no mock code in dist/.`);
