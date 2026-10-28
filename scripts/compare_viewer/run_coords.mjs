// node run_coords.mjs <coordinate-notation src/index.ts> <cases.json> <out.json>
// Bundles the viewer's coordinate notations (coordinate-notation, with its mgrs) and runs each case.
import { build } from 'esbuild';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const [index, casesPath, outPath] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const dir = mkdtempSync(join(tmpdir(), 'viewer-coords-'));
const bundle = join(dir, 'bundle.mjs');
await build({ entryPoints: [resolve(index)], bundle: true, format: 'esm', platform: 'node', outfile: bundle, nodePaths: [join(here, 'node_modules')], logLevel: 'error' });
const c = await import(pathToFileURL(bundle).href);

const cases = JSON.parse(readFileSync(casesPath, 'utf8'));
const out = {
  parsed: cases.texts.map((t) => c.parseCoordinate(t)),
  written: cases.points.map((p) => [c.formatLatLon(p), c.formatDms(p), c.formatMgrs(p), c.formatUtm(p), c.utmZone(p)]),
};
writeFileSync(outPath, JSON.stringify(out));
