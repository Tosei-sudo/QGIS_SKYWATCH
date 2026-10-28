// node run_color.mjs <browser-image-enhancement dist folder> <cases.json> <out.json>
// Colour scales and classes through the library's own functions (color-scale.js, color-classes.js, raster.js).
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [dist, casesPath, outPath] = process.argv.slice(2);
const lib = async (p) => import(pathToFileURL(resolve(dist, p)).href);
const { colorScalePalette, applyPalette } = await lib('color-scale.js');
const { colorClasses, classPalette, classRange, classShares } = await lib('color-classes.js');
const { sampleRasterHistogram } = await lib('raster.js');

const c = JSON.parse(readFileSync(casesPath, 'utf8'));
const out = { palettes: {}, applied: {}, classes: {} };
for (const p of c.palettes) out.palettes[p.name] = Array.from(colorScalePalette(p.scale, p.options));
for (const a of c.applied) {
  const data = Uint8ClampedArray.from(Buffer.from(a.image, 'base64'));
  out.applied[a.name] = Buffer.from(applyPalette(data, colorScalePalette(a.scale, a.options))).toString('base64');
}
for (const k of c.classes) {
  const raster = { data: Float64Array.from(k.raster.data), width: k.raster.width, height: k.raster.height, bands: 1, noData: k.raster.noData ?? null };
  const hist = k.hist8 ? { range: [0, 255], bins: [Float64Array.from(k.hist8)], count: k.hist8.reduce((a, b) => a + b, 0) } : sampleRasterHistogram([{ raster }]);
  const classes = colorClasses(hist, k.options);
  out.classes[k.name] = classes && { classes, range: classRange(classes), palette: Array.from(classPalette(classes)), shares: classShares(hist, classes) };
}
writeFileSync(outPath, JSON.stringify(out));
