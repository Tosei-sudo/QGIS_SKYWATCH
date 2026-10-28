// node run_pansharpen.mjs <browser-image-enhancement dist folder> <cases.json> <out.json>
// Pan-sharpening through the library's own panSharpen (pansharpen.js).
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [dist, casesPath, outPath] = process.argv.slice(2);
const { panSharpen } = await import(pathToFileURL(resolve(dist, 'pansharpen.js')).href);
const types = { uint8: Uint8Array, uint16: Uint16Array, int16: Int16Array, float32: Float32Array };
const out = {};
for (const c of JSON.parse(readFileSync(casesPath, 'utf8'))) {
  const Pan = types[c.panType];
  const Ms = types[c.msType];
  const pan = { data: Pan.from(c.pan, (v) => v ?? NaN), width: c.width, height: c.height, noData: c.panNoData ?? null };
  const ms = { data: Ms.from(c.ms, (v) => v ?? NaN), width: c.width, height: c.height, bands: c.bands, noData: c.msNoData ?? null, alpha: !!c.alpha };
  const r = panSharpen(pan, ms, c.options);
  out[c.name] = { data: Array.from(r.data, (v) => (Number.isNaN(v) ? null : v)), noData: Number.isNaN(r.noData) ? null : r.noData, model: r.model };
}
writeFileSync(outPath, JSON.stringify(out));
