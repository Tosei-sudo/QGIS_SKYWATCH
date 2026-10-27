// node run_viewer.mjs <viewer src folder> <cases.json> <out.json>
// Bundles the viewer's imaging-plan.ts (with satellite.js) and runs each case through planAccess and coverPlan.
import { build } from 'esbuild';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const [src, casesPath, outPath] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const dir = mkdtempSync(join(tmpdir(), 'viewer-plan-'));
const entry = join(dir, 'entry.ts');
writeFileSync(
  entry,
  `export { planAccess, coverPlan, satellitesFromText, shiftScene, passShapes } from ${JSON.stringify(resolve(src, 'imaging-plan.ts'))};
   export { withModes } from ${JSON.stringify(resolve(src, 'sensor-modes.ts'))};
   export { stereoPairs, repeatPairs } from ${JSON.stringify(resolve(src, 'imaging-plan-pairs.ts'))};
   export { boxOf, gridCells, cellTargets, revisitStats } from ${JSON.stringify(resolve(src, 'imaging-plan-revisit.ts'))};`,
);
const bundle = join(dir, 'bundle.mjs');
await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', outfile: bundle, nodePaths: [join(here, 'node_modules')], logLevel: 'error' });
const viewer = await import(pathToFileURL(bundle).href);

const cases = JSON.parse(readFileSync(casesPath, 'utf8'));
const out = {};
for (const c of cases) {
  const sats = c.satellites.map((s) => ({ ...viewer.satellitesFromText(s.tle, { maxOffNadir: 30, minOffNadir: 0, swath: 10 })[0], ...s.spec }));
  const cells = c.revisit ? viewer.gridCells(viewer.boxOf(c.targets), c.revisit.size) : null;
  const targets = cells ? viewer.cellTargets(cells) : c.targets;
  const t0 = performance.now();
  const result = viewer.planAccess(sats, targets, c.options);
  const ms = performance.now() - t0;
  const plain = (op) => ({
    satellite: op.satellite, target: op.target, time: op.time, offNadir: op.offNadir, incidence: op.incidence, elevation: op.elevation,
    side: op.side, ascending: op.ascending, sunElevation: op.sunElevation, coverage: op.coverage, width: op.width, strips: op.strips, frames: op.frames,
    window: op.window, along: op.along, azimuth: op.azimuth,
    ...(op.downlink ? { downlink: [op.downlink.time, op.downlink.station] } : {}),
    ...(op.shot !== undefined ? { shot: op.shot, gain: op.gain, cumulative: op.cumulative, shotTargets: op.shotTargets } : {}),
  });
  const entryOut = { opportunities: result.opportunities.map(plain), problems: result.problems, tooSoon: result.tooSoon, ms };
  if (c.cover) {
    const plans = viewer.coverPlan(result.opportunities, c.targets, sats, c.cover.goal, c.cover.minGain, c.cover.share);
    entryOut.cover = plans.map((p) => ({ target: p.target, coverage: p.coverage, done: p.done, blocked: p.blocked, steps: p.steps.map(plain) }));
  }
  const pairOut = (p) => ({ a: p.a.time, b: p.b.time, sa: p.a.satellite, sb: p.b.satellite, target: p.a.target, convergence: p.convergence, bh: p.bh, days: p.days, baseline: p.baseline });
  if (c.pairs?.stereo) entryOut.stereo = viewer.stereoPairs(result.opportunities, sats, targets, c.pairs.stereo).map(pairOut);
  if (c.pairs?.repeat) entryOut.repeat = viewer.repeatPairs(result.opportunities, sats, targets, c.pairs.repeat).map(pairOut);
  if (cells) entryOut.revisit = viewer.revisitStats(result.opportunities, cells.length, c.options.start, c.options.end);
  out[c.name] = entryOut;
}
writeFileSync(outPath, JSON.stringify(out));
