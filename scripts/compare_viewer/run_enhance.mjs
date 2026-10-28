// node run_enhance.mjs <browser-image-enhancement dist folder> <cases.json> <out.json>
// Runs each case through the library's own engine (normalizeOp → resolveForPixels → compile → processPixels)
// and its raw-value stretch (sampleRasterHistogram → computeRasterStretch).
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [dist, casesPath, outPath] = process.argv.slice(2);
const lib = async (p) => import(pathToFileURL(resolve(dist, p)).href);
const { normalizeOp } = await lib('ops/index.js');
const { resolveForPixels } = await lib('core/histogram.js');
const { compile, processPixels, resolveMode } = await lib('core/process.js');
const { sampleRasterHistogram, computeRasterStretch } = await lib('raster.js');

const cases = JSON.parse(readFileSync(casesPath, 'utf8'));
const out = {};
for (const c of cases) {
  if (c.raster) {
    const data = Float64Array.from(c.raster.data);
    const raster = { data, width: c.raster.width, height: c.raster.height, bands: c.raster.bands, noData: c.raster.noData ?? null };
    const stats = sampleRasterHistogram([{ raster }]);
    out[c.name] = { range: stats.range, stretch: computeRasterStretch(stats, c.options ?? {}) };
    continue;
  }
  const src = Uint8ClampedArray.from(Buffer.from(c.image, 'base64'));
  const mode = resolveMode(src, c.colorMode ?? 'auto');
  const ops = resolveForPixels(c.ops.map(normalizeOp), src, mode);
  const dst = new Uint8ClampedArray(src.length);
  processPixels(src, dst, compile(ops, mode), c.width);
  out[c.name] = { image: Buffer.from(dst).toString('base64'), ops };
}
writeFileSync(outPath, JSON.stringify(out));
