import assert from "node:assert/strict";
import test from "node:test";
import { inflateSync } from "node:zlib";

import { parseColor, parsePath, parseSvg, parseTransform, svgSize, svgToPdf, textRuns, textWidth } from "../figure-export.mjs";
import { LIGHT_CANVAS } from "../canvas-theme.mjs";
import { locusCompareSVG } from "../locuscompare.mjs";

/** The PDF's objects, with streams inflated. */
async function readPdf(blob) {
  const bytes = Buffer.from(await blob.arrayBuffer());
  const text = bytes.toString("latin1");
  const objects = new Map();
  for (const m of text.matchAll(/(\d+) 0 obj\n([\s\S]*?)\nendobj/g)) {
    let body = m[2];
    const stream = body.match(/^(<<[\s\S]*?>>)\nstream\n/);
    if (stream) {
      const start = m.index + m[0].indexOf("stream\n") + 7;
      const length = Number(stream[1].match(/\/Length (\d+)/)[1]);
      const data = bytes.subarray(start, start + length);
      body = { dict: stream[1], data: /FlateDecode/.test(stream[1]) ? inflateSync(data) : data };
    }
    objects.set(Number(m[1]), body);
  }
  return { text, objects };
}

const contentOf = (pdf) => [...pdf.objects.values()].find((o) => typeof o === "object" && !/\/Subtype/.test(o.dict)).data.toString("latin1");

test("SVG parsing keeps elements, attributes, entities and ids", () => {
  const { svg, ids } = parseSvg('<?xml version="1.0"?><!-- c --><svg xmlns="http://www.w3.org/2000/svg" width="10" height="20"><g id="a"><text x="1">A &amp; B &#x394;</text><rect width="5" height="5"/></g></svg>');
  assert.equal(svg.name, "svg");
  assert.equal(ids.get("a").children.length, 2);
  assert.equal(ids.get("a").children[0].children[0].text, "A & B Δ");
  assert.deepEqual(svgSize(svg), { width: 10, height: 20 });
  assert.deepEqual(svgSize('<svg viewBox="0 0 300 100"></svg>'), { width: 300, height: 100 });
});

test("colors, transforms and paths are read as browsers do", () => {
  assert.deepEqual(parseColor("#fff"), [1, 1, 1, 1]);
  assert.deepEqual(parseColor("rgba(255, 0, 0, 0.5)"), [1, 0, 0, 0.5]);
  assert.equal(parseColor("none"), null);
  assert.deepEqual(parseColor("steelblue").map((x) => Math.round(x * 255)), [70, 130, 180, 255]);
  const m = parseTransform("translate(10, 5) scale(2)");
  assert.deepEqual(m, [2, 0, 0, 2, 10, 5]);
  const r = parseTransform("rotate(90)");
  assert.ok(Math.abs(r[0]) < 1e-12 && Math.abs(r[1] - 1) < 1e-12);
  const path = parsePath("M10 10 h5 v5 l-5 0 z m 1 1 L 2 2");
  assert.deepEqual(path.slice(0, 4), [["M", 10, 10], ["L", 15, 10], ["L", 15, 15], ["L", 10, 15]]);
  assert.deepEqual(path[5], ["M", 11, 11]);
  // a half circle of radius 10 as cubic curves that end where the arc ends
  const arc = parsePath("M0 0 A10 10 0 0 1 20 0");
  assert.equal(arc[arc.length - 1][0], "C");
  assert.deepEqual(arc[arc.length - 1].slice(5), [20, 0]);
  assert.ok(arc.every((s) => s[0] !== "C" || s.slice(1).every(Number.isFinite)));
  // arc flags written without separators
  assert.deepEqual(parsePath("M0 0a5 5 0 105 5").at(-1).slice(5), [5, 5]);
});

test("text is set in Arial with Arial's widths; other characters come from Symbol or as ?", () => {
  assert.equal(textWidth("A", 10), 6.67);
  assert.equal(textWidth("A", 10, true), 7.22);
  const runs = textRuns("Δ −log₁₀ P 中");
  assert.deepEqual(runs.map((r) => r.font), ["Sym", "F1", "Sym", "F1", "F1", "F1"]);
  assert.equal(runs.find((r) => r.script === "sub").bytes, "10");
  assert.ok(runs[runs.length - 1].bytes.endsWith("?"));
});

test("a figure becomes a one-page PDF with vector shapes, text and transparency", async () => {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
    <defs><clipPath id="c"><rect x="0" y="0" width="50" height="50"/></clipPath></defs>
    <rect width="100%" height="100%" fill="none"/>
    <g clip-path="url(#c)" transform="translate(5,5)"><circle cx="20" cy="20" r="30" fill="rgba(200,0,0,0.5)"/></g>
    <line x1="0" y1="90" x2="200" y2="90" stroke="#333" stroke-dasharray="3 2"/>
    <text x="100" y="50" text-anchor="middle" font-family="Arial" font-weight="bold" font-size="12">Gene (x)</text>
  </svg>`;
  const pdf = await readPdf(await svgToPdf(svg, { decodeImage: null }));
  assert.match(pdf.text, /^%PDF-1\.4/);
  assert.match(pdf.text, /\/MediaBox \[0 0 150 75\]/);  // 1 px = 0.75 pt
  assert.match(pdf.text, /\/BaseFont \/Arial-BoldMT/);
  assert.match(pdf.text, /\/ca 0\.5/);
  assert.match(pdf.text, /startxref\n\d+\n%%EOF\n$/);
  const content = contentOf(pdf);
  assert.match(content, /W n/);                   // the clip path
  assert.match(content, /\[3 2\] 0 d/);           // the dash
  assert.match(content, /\(Gene \\\(x\\\)\) Tj/); // escaped text
  // the text is centered on x = 100: it starts half its width to the left
  const x = Number(content.match(/1 0 0 -1 ([\d.]+) 50 Tm/)[1]);
  assert.ok(Math.abs(x - (100 - textWidth("Gene (x)", 12, true) / 2)) < 0.01);
  // the full-size rectangle without fill draws nothing
  assert.doesNotMatch(content, /0 0 m\n200 0 l/);
  // the xref offsets point at the objects
  const offsets = [...pdf.text.match(/xref\n0 \d+\n0000000000 65535 f \n([\s\S]*?)trailer/)[1].matchAll(/(\d{10}) 00000 n/g)].map((m) => Number(m[1]));
  offsets.forEach((o, i) => assert.equal(pdf.text.slice(o, o + String(i + 1).length + 6), `${i + 1} 0 obj`));
});

test("embedded images become image objects with their transparency", async () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"><image x="0" y="0" width="20" height="10" href="data:x"/></svg>';
  const rgba = new Uint8Array([255, 0, 0, 255, 0, 0, 255, 128]);
  const pdf = await readPdf(await svgToPdf(svg, { decodeImage: async () => ({ width: 2, height: 1, rgba }) }));
  assert.match(pdf.text, /\/Subtype \/Image \/Width 2 \/Height 1/);
  assert.match(pdf.text, /\/SMask \d+ 0 R/);
  assert.match(contentOf(pdf), /\/Im1 Do/);
});

test("the LocusCompare figure converts with every point and label", async () => {
  const snps = Array.from({ length: 40 }, (_, i) => ({ id: `rs${i}`, rsid: `rs${i}`, pos: 1000 + i * 100, ref: "A", alt: "G",
    p1: 10 ** -(i % 7), logp1: i % 7, beta1: 0.1, p2: 10 ** -(i % 5), logp2: i % 5, beta2: 0.1, r2: (i % 10) / 10 }));
  const data = { region: { chrom: "chr1", start: 1000, end: 5000 }, gwas: { label: "CAD", subset: "", subsets: [] },
    eqtl: { symbol: "GENE1", gene: "G1", tissueLabel: "Artery", tissue: "a" }, ld: { available: true, label: "EUR" }, lead: "rs3", snps,
    genes: [{ id: "G1", symbol: "GENE1", start: 1500, end: 4000, strand: "+" }] };
  const figure = locusCompareSVG(data, { theme: LIGHT_CANVAS, accent: "#4c7a86" });
  const content = contentOf(await readPdf(await svgToPdf(figure.svg, { decodeImage: null })));
  const circles = (figure.svg.match(/<circle\b/g) || []).length;
  assert.ok(circles >= 80);
  assert.ok((content.match(/ c\n/g) || []).length >= circles * 4);
  assert.match(content, /\(GENE1/);
});
