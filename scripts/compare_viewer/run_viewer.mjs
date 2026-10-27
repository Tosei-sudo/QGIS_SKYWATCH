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
   export { boxOf, gridCells, cellTargets, revisitStats } from ${JSON.stringify(resolve(src, 'imaging-plan-revisit.ts'))};
   export { shipWindow, sampleTargets, refineShots } from ${JSON.stringify(resolve(src, 'imaging-plan-moving.ts'))};
   export { satelliteTrack } from ${JSON.stringify(resolve(src, 'satellite-track.ts'))};`,
);
const bundle = join(dir, 'bundle.mjs');
await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', outfile: bundle, nodePaths: [join(here, 'node_modules')], logLevel: 'error' });
const viewer = await import(pathToFileURL(bundle).href);

const cases = JSON.parse(readFileSync(casesPath, 'utf8'));
const out = {};
for (const c of cases) {
  const sats = c.satellites.map((s) => ({ ...viewer.satellitesFromText(s.tle, { maxOffNadir: 30, minOffNadir: 0, swath: 10 })[0], ...s.spec }));
  if (c.track) {
    // The satellite pass dock: each satellite's track and reach.
    const t0 = performance.now();
    const tracks = sats.map((sat) => viewer.satelliteTrack(sat, c.track.from, c.track.to, c.track.options ?? {}));
    out[c.name] = { ms: performance.now() - t0, tracks: tracks.map((t) => ({ times: t.times, track: t.track, heights: t.heights, ascending: t.ascending, open: t.open, offNadir: t.offNadir, bands: t.bands })) };
    continue;
  }
  const cells = c.revisit ? viewer.gridCells(viewer.boxOf(c.targets), c.revisit.size) : null;
  const targets = cells ? viewer.cellTargets(cells) : c.targets;
  if (c.ships) {
    // Moving targets: the hourly places planned, then each pass aimed at the circle of its moment.
    const t0 = performance.now();
    const span = viewer.shipWindow(c.ships, c.options);
    const planned = { ...c.options, ...span };
    const { targets: places, samples } = viewer.sampleTargets(c.ships, span.start, span.end);
    const first = viewer.planAccess(sats, places, planned);
    const shots = viewer.refineShots(first, samples, c.ships, sats, planned);
    out[c.name] = {
      ms: performance.now() - t0,
      problems: first.problems,
      tooSoon: first.tooSoon,
      shots: shots.map((s) => ({ ship: s.ship, time: s.op.time, satellite: s.op.satellite, offNadir: s.op.offNadir, incidence: s.op.incidence, elevation: s.op.elevation,
        sunElevation: s.op.sunElevation, coverage: s.op.coverage, side: s.op.side, ascending: s.op.ascending, center: s.target.center, uncertainty: s.target.uncertainty })),
    };
    continue;
  }
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
