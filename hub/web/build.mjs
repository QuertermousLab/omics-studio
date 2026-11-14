// Builds the hub pages into ../static: bundles (esbuild), stylesheets and HTML.
//   cd hub/web && npm install && npm run build
import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { copyFileSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';

const out = new URL('../static/', import.meta.url).pathname;
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });

await build({
  entryPoints: ['src/hub-page.ts', 'src/viewer-page.ts', 'src/crispr-page.ts', 'src/workbench-page.ts'],
  outdir: out, bundle: true, splitting: true, format: 'esm', target: 'es2022', minify: true, sourcemap: true,
  chunkNames: 'chunk-[hash]', legalComments: 'linked', logLevel: 'warning',
});

const read = (f) => readFileSync(new URL(f, import.meta.url), 'utf8');
const css = { hub: ['theme', 'nav', 'hub'], viewer: ['theme', 'nav', 'manager', 'shared', 'viewer'], crispr: ['theme', 'nav', 'manager', 'shared', 'crispr'],
  workbench: ['theme', 'nav', 'manager', 'hub', 'workbench'] };
for (const [name, parts] of Object.entries(css)) writeFileSync(`${out}${name}.css`, parts.map((p) => read(`src/${p}.css`)).join('\n'));

const hash = createHash('sha1');
for (const f of readdirSync(out).sort()) hash.update(readFileSync(out + f));
const version = hash.digest('hex').slice(0, 10);
const head = read('pages/head.html'), nav = read('pages/nav.html');
for (const page of ['hub', 'viewer', 'crispr', 'workbench']) {
  const html = read(`pages/${page}.html`)
    .replace('<!--HEAD-->', head).replace('<!--NAV-->', nav)
    .replace(/<!--BODY:([\w.]+)-->/, (_, f) => read(`pages/${f}`))
    .replaceAll('__BUILD__', version)
    // each part's icon: Omics Studio (home), Omics Workbench, Omics Canvas (the viewers)
    .replaceAll('__ICON__', { hub: 'studio.svg', workbench: 'workbench.svg' }[page] ?? 'favicon.svg');
  writeFileSync(`${out}${page}.html`, html);
}
for (const icon of ['favicon.svg', 'studio.svg', 'workbench.svg']) copyFileSync(new URL(`pages/${icon}`, import.meta.url), `${out}${icon}`);
// Genome Canvas's fonts (SIL OFL), so every page uses the same type
const fonts = new URL('../../genome-canvas/vendor/fonts/', import.meta.url).pathname;
mkdirSync(`${out}fonts`);
for (const f of readdirSync(fonts)) copyFileSync(fonts + f, `${out}fonts/${f}`);
// the heading face, when this server has it (state/fonts/: local, not in git); the pages fall back to Fraunces
const heading = new URL('../../state/fonts/heading-serif.ttf', import.meta.url).pathname;
try { copyFileSync(heading, `${out}fonts/heading-serif.ttf`); } catch { /* not on this server */ }
console.log(`built ${version}: ${readdirSync(out).join(' ')}`);
