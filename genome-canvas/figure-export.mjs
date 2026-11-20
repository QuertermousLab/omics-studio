// Figure export shared by Genome Canvas, GREAT and the hub's viewers and plots: a figure drawn as SVG is saved as
// SVG, PNG or vector PDF. svgToPdf() turns the SVG these pages write (and IGV's canvas2svg output) into a one-page PDF
// with the same paths, text and embedded raster images, without any library.
//
// Text is set in Arial (the rule for exported figures): ArialMT / Arial-BoldMT, not embedded (Arial is proprietary),
// with Arial's widths (Helvetica's, which are the same), so Illustrator, Preview and Acrobat use the installed Arial.
// Characters outside Windows-1252 go to the Symbol font when it has them (Greek letters, −, ≤, ≥, →, …), others
// become "?". One CSS pixel is 0.75 pt, as when a browser prints.

// ------------------------------------------------------------------ fonts

// advance widths (1/1000 em) of WinAnsi codes 32..255: Arial regular and bold (= Helvetica's), and Symbol codes 32..255
const W_REGULAR = [278,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,278,278,584,584,584,556,1015,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,278,278,278,469,556,333,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,334,260,334,584,350,556,350,222,556,333,1000,556,556,333,1000,667,333,1000,350,611,350,350,222,222,333,333,350,556,1000,333,1000,500,333,944,350,500,667,278,333,556,556,556,556,260,556,333,737,370,556,584,333,737,333,400,584,333,333,333,556,537,278,333,333,365,556,834,834,834,611,667,667,667,667,667,667,1000,722,667,667,667,667,278,278,278,278,722,722,778,778,778,778,778,584,778,722,722,722,722,667,667,611,556,556,556,556,556,556,889,500,556,556,556,556,278,278,278,278,556,556,556,556,556,556,556,584,611,556,556,556,556,500,556,500];
const W_BOLD = [278,333,474,556,556,889,722,238,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,333,333,584,584,584,611,975,722,722,722,722,667,611,778,722,278,556,722,611,833,722,778,667,778,722,667,611,722,667,944,667,667,611,333,278,333,584,556,333,556,611,556,611,556,333,611,611,278,278,556,278,889,611,611,611,611,389,556,333,611,556,778,556,556,500,389,280,389,584,350,556,350,278,556,500,1000,556,556,333,1000,667,333,1000,350,611,350,350,278,278,500,500,350,556,1000,333,1000,556,333,944,350,500,667,278,333,556,556,556,556,280,556,333,737,370,556,584,333,737,333,400,584,333,333,333,611,556,278,333,333,365,556,834,834,834,611,722,722,722,722,722,722,1000,722,667,667,667,667,278,278,278,278,722,722,778,778,778,778,778,584,778,722,722,722,722,667,667,611,556,556,556,556,556,556,889,556,556,556,556,556,278,278,278,278,611,611,611,611,611,611,611,584,611,611,611,611,611,556,611,556];
const W_SYMBOL = [250,333,713,500,549,833,778,439,333,333,500,549,250,549,250,278,500,500,500,500,500,500,500,500,500,500,278,278,549,549,549,444,549,722,667,722,612,611,763,603,722,333,631,722,686,889,722,722,768,741,556,592,611,690,439,768,645,795,611,333,863,333,658,500,500,631,549,549,494,439,521,411,603,329,603,549,549,576,521,549,549,521,549,603,439,576,713,686,493,686,494,480,200,480,549,0,790,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,750,620,247,549,167,713,500,753,753,753,753,1042,987,603,987,603,400,549,411,549,549,713,494,460,549,549,549,549,1000,603,1000,658,823,686,795,987,768,768,823,768,768,713,713,713,713,713,713,713,768,713,790,790,890,823,549,250,713,603,603,1042,987,603,987,603,494,329,790,790,786,713,384,384,384,384,384,384,494,494,494,494,0,329,274,686,686,686,384,384,384,384,384,384,494,494,494,494,0];

const FONTS = {
  F1: { name: "ArialMT", widths: W_REGULAR, flags: 32, italic: 0, stem: 80, bbox: "-665 -325 2000 1006" },
  F2: { name: "Arial-BoldMT", widths: W_BOLD, flags: 262176, italic: 0, stem: 120, bbox: "-628 -376 2000 1018" },
  F3: { name: "Arial-ItalicMT", widths: W_REGULAR, flags: 96, italic: -12, stem: 80, bbox: "-517 -325 1359 998" },
  F4: { name: "Arial-BoldItalicMT", widths: W_BOLD, flags: 262240, italic: -12, stem: 120, bbox: "-560 -376 1390 1018" },
};

// Unicode -> WinAnsi codes 128..159 (the rest of 160..255 is Latin-1)
const WIN_ANSI = new Map([[0x20ac, 128], [0x201a, 130], [0x192, 131], [0x201e, 132], [0x2026, 133], [0x2020, 134], [0x2021, 135],
  [0x2c6, 136], [0x2030, 137], [0x160, 138], [0x2039, 139], [0x152, 140], [0x17d, 142], [0x2018, 145], [0x2019, 146], [0x201c, 147],
  [0x201d, 148], [0x2022, 149], [0x2013, 150], [0x2014, 151], [0x2dc, 152], [0x2122, 153], [0x161, 154], [0x203a, 155], [0x153, 156],
  [0x17e, 158], [0x178, 159]]);

// Unicode -> Symbol font code
const SYMBOL = new Map([
  ..."ΑΒΧΔΕΦΓΗΙΚΛΜΝΟΠΘΡΣΤΥΩΞΨΖαβχδεφγηικλμνοπθρστυωξψζςϑϕϖ".split("").map((c, i) => [c.codePointAt(0), "ABCDEFGHIKLMNOPQRSTUWXYZabcdefghiklmnopqrstuwxyzVJjv".charCodeAt(i)]),
  [0x2212, 0x2d], [0x2217, 0x2a], [0x2264, 0xa3], [0x2265, 0xb3], [0x2260, 0xb9], [0x2248, 0xbb], [0x221e, 0xa5], [0x2192, 0xae],
  [0x2190, 0xac], [0x2191, 0xad], [0x2193, 0xaf], [0x2194, 0xab], [0x2211, 0xe5], [0x221a, 0xd6], [0x2202, 0xb6], [0x2208, 0xce],
  [0x2229, 0xc7], [0x222a, 0xc8], [0x22c5, 0xd7], [0x2032, 0xa2], [0x2033, 0xb2], [0x221d, 0xb5], [0x223c, 0x7e], [0x2206, 0x44],
  [0x2261, 0xba], [0x2026, 0xbc], [0x2022, 0xb7],
]);

// superscript and subscript digits and signs (−log₁₀ P, 10⁻⁵): set as small characters above or below the baseline
const SCRIPTS = new Map([..."⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ"].map((c, i) => [c.codePointAt(0), ["sup", "0123456789+\u2212=()n"[i]]]).concat(
  [..."₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎"].map((c, i) => [c.codePointAt(0), ["sub", "0123456789+\u2212=()"[i]]])));
export const SCRIPT_SCALE = 0.65;

/**
 * Splits text into runs of one font: [{ font: "F1".."F4" | "Sym", bytes (latin-1 string), width (em/1000 of the text's
 * size), script ("sup" | "sub" | undefined: drawn at SCRIPT_SCALE of the size, raised or lowered) }].
 */
export function textRuns(text, bold = false, italic = false) {
  const main = bold ? (italic ? "F4" : "F2") : (italic ? "F3" : "F1");
  const widths = FONTS[main].widths;
  const runs = [];
  for (const original of String(text)) {
    let ch = original, script;
    if (SCRIPTS.has(ch.codePointAt(0))) [script, ch] = SCRIPTS.get(ch.codePointAt(0));
    const u = ch.codePointAt(0);
    let font = main, code;
    if ((u >= 32 && u <= 126) || (u >= 160 && u <= 255)) code = u;
    else if (WIN_ANSI.has(u)) code = WIN_ANSI.get(u);
    else if (SYMBOL.has(u)) { font = "Sym"; code = SYMBOL.get(u); }
    else if (u === 9 || u === 10 || u === 13) code = 32;
    else code = 63;  // "?"
    const width = (font === "Sym" ? W_SYMBOL[code - 32] : widths[code - 32]) * (script ? SCRIPT_SCALE : 1);
    const last = runs[runs.length - 1];
    if (last && last.font === font && last.script === script) { last.bytes += String.fromCharCode(code); last.width += width; }
    else runs.push({ font, bytes: String.fromCharCode(code), width, script });
  }
  return runs;
}

/** Width of text in Arial at the given size (px), as the PDF lays it out. */
export function textWidth(text, size, bold = false) {
  return textRuns(text, bold).reduce((sum, run) => sum + run.width, 0) * size / 1000;
}

// ------------------------------------------------------------------ SVG parsing

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
function decodeEntities(text) {
  return text.indexOf("&") < 0 ? text : text.replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (all, e) =>
    e[0] === "#" ? String.fromCodePoint(e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10)) : ENTITIES[e] ?? all);
}

/** A small XML parser for SVG text: { name, attrs, children } elements and { name: "#text", text } nodes. */
export function parseSvg(text) {
  const root = { name: "#document", attrs: {}, children: [] };
  const ids = new Map();
  const stack = [root];
  const token = /<!--[\s\S]*?-->|<!\[CDATA\[([\s\S]*?)\]\]>|<\?[\s\S]*?\?>|<!DOCTYPE[^>[]*(?:\[[\s\S]*?\])?\s*>|<\/([^\s>]+)\s*>|<([^\s/>]+)((?:\s+[^\s=/>]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>|([^<]+)/gi;
  const attribute = /([^\s=]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
  let m;
  while ((m = token.exec(text))) {
    const top = stack[stack.length - 1];
    if (m[3]) {
      const attrs = {};
      if (m[4]) for (const a of m[4].matchAll(attribute)) attrs[a[1]] = decodeEntities(a[2] ?? a[3] ?? "");
      const node = { name: m[3].replace(/^svg:/, ""), attrs, children: [], parent: top };
      top.children.push(node);
      if (attrs.id) ids.set(attrs.id, node);
      if (!m[5]) stack.push(node);
    } else if (m[2]) {
      const name = m[2].replace(/^svg:/, "");
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].name === name) { stack.length = i; break; }
    } else if (m[6] !== undefined) {
      if (/\S/.test(m[6]) || top.name === "text" || top.name === "tspan") top.children.push({ name: "#text", text: decodeEntities(m[6]) });
    } else if (m[1] !== undefined) {
      top.children.push({ name: "#text", text: m[1] });
    }
  }
  const svg = root.children.find((node) => node.name === "svg");
  if (!svg) throw new Error("Not an SVG document");
  return { svg, ids };
}

// ------------------------------------------------------------------ styles

const NAMED = {
  black: "000000", white: "ffffff", red: "ff0000", green: "008000", blue: "0000ff", gray: "808080", grey: "808080",
  silver: "c0c0c0", maroon: "800000", purple: "800080", fuchsia: "ff00ff", magenta: "ff00ff", lime: "00ff00", olive: "808000",
  yellow: "ffff00", navy: "000080", teal: "008080", aqua: "00ffff", cyan: "00ffff", orange: "ffa500", lightgray: "d3d3d3",
  lightgrey: "d3d3d3", darkgray: "a9a9a9", darkgrey: "a9a9a9", dimgray: "696969", dimgrey: "696969", gainsboro: "dcdcdc",
  whitesmoke: "f5f5f5", steelblue: "4682b4", darkblue: "00008b", darkred: "8b0000", darkgreen: "006400", brown: "a52a2a",
  pink: "ffc0cb", gold: "ffd700", indigo: "4b0082", violet: "ee82ee", salmon: "fa8072", tomato: "ff6347", coral: "ff7f50",
  crimson: "dc143c", orchid: "da70d6", tan: "d2b48c", khaki: "f0e68c", lightblue: "add8e6", skyblue: "87ceeb",
  royalblue: "4169e1", slategray: "708090", slategrey: "708090", firebrick: "b22222", forestgreen: "228b22",
  seagreen: "2e8b57", darkorange: "ff8c00", chocolate: "d2691e", sienna: "a0522d", turquoise: "40e0d0",
  lightgreen: "90ee90", limegreen: "32cd32", darkviolet: "9400d3", deepskyblue: "00bfff", dodgerblue: "1e90ff",
};

/** CSS color -> [r, g, b, a] with r, g, b, a in 0..1; null for none / transparent / unknown paint. */
export function parseColor(value) {
  if (!value) return null;
  const v = String(value).trim().toLowerCase();
  if (v === "none" || v === "transparent") return null;
  let hex = v[0] === "#" ? v.slice(1) : NAMED[v];
  if (hex) {
    if (hex.length === 3 || hex.length === 4) hex = hex.split("").map((c) => c + c).join("");
    if (!/^[0-9a-f]{6}([0-9a-f]{2})?$/.test(hex)) return [0, 0, 0, 1];
    const n = (i) => parseInt(hex.slice(i, i + 2), 16) / 255;
    return [n(0), n(2), n(4), hex.length === 8 ? n(6) : 1];
  }
  const fn = v.match(/^(rgba?|hsla?)\(([^)]*)\)$/);
  if (fn) {
    const parts = fn[2].split(/[\s,/]+/).filter(Boolean);
    const num = (p, scale) => (p.endsWith("%") ? parseFloat(p) / 100 : parseFloat(p) / scale);
    const alpha = parts.length > 3 ? Math.max(0, Math.min(1, num(parts[3], 1))) : 1;
    if (fn[1].startsWith("rgb")) return [num(parts[0], 255), num(parts[1], 255), num(parts[2], 255), alpha].map((x) => Math.max(0, Math.min(1, x || 0)));
    const h = (((parseFloat(parts[0]) % 360) + 360) % 360) / 360, s = parseFloat(parts[1]) / 100, l = parseFloat(parts[2]) / 100;
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
    const hue = (t) => { t = (t + 1) % 1; return t < 1 / 6 ? p + (q - p) * 6 * t : t < 1 / 2 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p; };
    return [hue(h + 1 / 3), hue(h), hue(h - 1 / 3), alpha];
  }
  return [0, 0, 0, 1];
}

const INHERITED = ["fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap", "stroke-linejoin",
  "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit", "font-family", "font-size", "font-weight", "font-style", "text-anchor",
  "visibility", "color", "paint-order", "dominant-baseline", "clip-rule", "image-rendering"];
const OWN = ["opacity", "display", "clip-path", "alignment-baseline", "baseline-shift", "stop-color", "stop-opacity"];
const PRESENTATION = new Set([...INHERITED, ...OWN]);

const DEFAULT_STYLE = {
  fill: "black", "fill-opacity": "1", "fill-rule": "nonzero", stroke: "none", "stroke-width": "1", "stroke-opacity": "1",
  "stroke-linecap": "butt", "stroke-linejoin": "miter", "stroke-dasharray": "none", "stroke-dashoffset": "0", "stroke-miterlimit": "4",
  "font-family": "Arial", "font-size": "16", "font-weight": "normal", "font-style": "normal", "text-anchor": "start",
  visibility: "visible", color: "black", "paint-order": "normal", "dominant-baseline": "auto", "clip-rule": "nonzero",
  "image-rendering": "auto", alpha: 1,
};

function parseFontShorthand(value, out) {
  // [style] [variant] [weight] size[/line-height] family
  const m = String(value).match(/^\s*((?:(?:italic|oblique|normal|bold|bolder|lighter|small-caps|\d{3})\s+)*)([\d.]+(?:px|pt|em)?)(?:\/\S+)?\s+(.+)$/i);
  if (!m) return;
  for (const word of m[1].split(/\s+/).filter(Boolean)) {
    if (/^(italic|oblique)$/i.test(word)) out["font-style"] = word;
    else if (/^(bold|bolder|lighter|\d{3})$/i.test(word)) out["font-weight"] = word;
  }
  out["font-size"] = m[2];
  out["font-family"] = m[3];
}

function computeStyle(node, parent) {
  const style = {};
  for (const key of INHERITED) style[key] = parent[key];
  style.alpha = parent.alpha;
  style.fontPx = parent.fontPx;
  const own = {};
  for (const [key, value] of Object.entries(node.attrs)) if (PRESENTATION.has(key)) own[key] = value;
  if (node.attrs.font) parseFontShorthand(node.attrs.font, own);
  if (node.attrs.style) for (const decl of node.attrs.style.split(";")) {
    const i = decl.indexOf(":");
    if (i < 0) continue;
    const key = decl.slice(0, i).trim().toLowerCase(), value = decl.slice(i + 1).replace(/!important/, "").trim();
    if (key === "font") parseFontShorthand(value, own);
    else if (PRESENTATION.has(key)) own[key] = value;
  }
  for (const [key, value] of Object.entries(own)) if (value !== "inherit") style[key] = value;
  if (own["font-size"]) style.fontPx = length(own["font-size"], parent.fontPx, parent.fontPx);
  style.alpha = parent.alpha * (own.opacity !== undefined ? clamp01(parseFloat(own.opacity)) : 1);
  style.display = own.display;
  style["clip-path"] = own["clip-path"];
  style["alignment-baseline"] = own["alignment-baseline"];
  style["baseline-shift"] = own["baseline-shift"];
  return style;
}

const clamp01 = (x) => (Number.isFinite(x) ? Math.max(0, Math.min(1, x)) : 1);

/** An SVG length in user units (px); `em` is the font size, `%` of `percentOf`. */
function length(value, fontPx = 16, percentOf = 0) {
  if (value === undefined || value === null || value === "") return 0;
  const m = String(value).trim().match(/^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)\s*(px|pt|pc|mm|cm|in|em|ex|%)?$/i);
  if (!m) return parseFloat(value) || 0;
  const n = parseFloat(m[1]);
  switch ((m[2] || "").toLowerCase()) {
    case "pt": return n * 4 / 3;
    case "pc": return n * 16;
    case "mm": return n * 96 / 25.4;
    case "cm": return n * 96 / 2.54;
    case "in": return n * 96;
    case "em": return n * fontPx;
    case "ex": return n * fontPx * 0.52;
    case "%": return n / 100 * percentOf;
    default: return n;
  }
}

const numbers = (value) => (String(value ?? "").match(/[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?/gi) || []).map(Number);

// ------------------------------------------------------------------ geometry

const IDENTITY = [1, 0, 0, 1, 0, 0];
const multiply = (a, b) => [a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
  a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5]];

export function parseTransform(text) {
  let matrix = IDENTITY;
  for (const m of String(text || "").matchAll(/(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)/g)) {
    const n = numbers(m[2]);
    let t;
    switch (m[1]) {
      case "matrix": t = n.length === 6 ? n : IDENTITY; break;
      case "translate": t = [1, 0, 0, 1, n[0] || 0, n[1] || 0]; break;
      case "scale": t = [n[0] ?? 1, 0, 0, n[1] ?? n[0] ?? 1, 0, 0]; break;
      case "rotate": {
        const a = (n[0] || 0) * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
        t = [c, s, -s, c, 0, 0];
        if (n.length >= 3) t = multiply(multiply([1, 0, 0, 1, n[1], n[2]], t), [1, 0, 0, 1, -n[1], -n[2]]);
        break;
      }
      case "skewX": t = [1, 0, Math.tan((n[0] || 0) * Math.PI / 180), 1, 0, 0]; break;
      case "skewY": t = [1, Math.tan((n[0] || 0) * Math.PI / 180), 0, 1, 0, 0]; break;
    }
    matrix = multiply(matrix, t);
  }
  return matrix;
}

const KAPPA = 0.5522847498;

/** SVG path data -> absolute segments: ["M", x, y] | ["L", x, y] | ["C", x1, y1, x2, y2, x, y] | ["Z"]. */
export function parsePath(d) {
  const out = [];
  const tokens = String(d || "").match(/[a-df-z]|[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?/gi) || [];
  let i = 0, cmd = "", x = 0, y = 0, sx = 0, sy = 0, cx = 0, cy = 0, qx = 0, qy = 0, prev = "";
  const num = () => Number(tokens[i++]);
  const flag = () => {  // arc flags may be written without separators ("a1 1 0 01 1 1")
    const t = tokens[i];
    if (t.length > 1 && (t[0] === "0" || t[0] === "1")) { tokens[i] = t.slice(1); return Number(t[0]); }
    i++;
    return Number(t);
  };
  while (i < tokens.length) {
    if (/[a-z]/i.test(tokens[i])) cmd = tokens[i++];
    else if (!cmd) { i++; continue; }
    const rel = cmd === cmd.toLowerCase() && cmd !== "z";
    const ox = rel ? x : 0, oy = rel ? y : 0;
    switch (cmd.toUpperCase()) {
      case "M": x = ox + num(); y = oy + num(); sx = x; sy = y; out.push(["M", x, y]); cmd = rel ? "l" : "L"; break;
      case "L": x = ox + num(); y = oy + num(); out.push(["L", x, y]); break;
      case "H": x = ox + num(); out.push(["L", x, y]); break;
      case "V": y = oy + num(); out.push(["L", x, y]); break;
      case "C": {
        const x1 = ox + num(), y1 = oy + num(), x2 = ox + num(), y2 = oy + num();
        x = ox + num(); y = oy + num(); cx = x2; cy = y2; out.push(["C", x1, y1, x2, y2, x, y]); break;
      }
      case "S": {
        const x1 = /[CS]/i.test(prev) ? 2 * x - cx : x, y1 = /[CS]/i.test(prev) ? 2 * y - cy : y;
        const x2 = ox + num(), y2 = oy + num();
        x = ox + num(); y = oy + num(); cx = x2; cy = y2; out.push(["C", x1, y1, x2, y2, x, y]); break;
      }
      case "Q": {
        const x1 = ox + num(), y1 = oy + num(), ex = ox + num(), ey = oy + num();
        out.push(["C", x + 2 / 3 * (x1 - x), y + 2 / 3 * (y1 - y), ex + 2 / 3 * (x1 - ex), ey + 2 / 3 * (y1 - ey), ex, ey]);
        qx = x1; qy = y1; x = ex; y = ey; break;
      }
      case "T": {
        const x1 = /[QT]/i.test(prev) ? 2 * x - qx : x, y1 = /[QT]/i.test(prev) ? 2 * y - qy : y, ex = ox + num(), ey = oy + num();
        out.push(["C", x + 2 / 3 * (x1 - x), y + 2 / 3 * (y1 - y), ex + 2 / 3 * (x1 - ex), ey + 2 / 3 * (y1 - ey), ex, ey]);
        qx = x1; qy = y1; x = ex; y = ey; break;
      }
      case "A": {
        const rx = num(), ry = num(), rotation = num(), large = flag(), sweep = flag(), ex = ox + num(), ey = oy + num();
        out.push(...arcToCurves(x, y, rx, ry, rotation, large, sweep, ex, ey));
        x = ex; y = ey; break;
      }
      case "Z": out.push(["Z"]); x = sx; y = sy; cmd = ""; break;
      default: i++;
    }
    prev = cmd || "Z";
    if (tokens.length && Number.isNaN(x + y)) break;
  }
  return out;
}

function arcToCurves(x1, y1, rx, ry, angle, large, sweep, x2, y2) {
  if (x1 === x2 && y1 === y2) return [];
  rx = Math.abs(rx); ry = Math.abs(ry);
  if (!rx || !ry) return [["L", x2, y2]];
  const phi = angle * Math.PI / 180, cos = Math.cos(phi), sin = Math.sin(phi);
  const dx = (x1 - x2) / 2, dy = (y1 - y2) / 2;
  const px = cos * dx + sin * dy, py = -sin * dx + cos * dy;
  const lambda = (px * px) / (rx * rx) + (py * py) / (ry * ry);
  if (lambda > 1) { rx *= Math.sqrt(lambda); ry *= Math.sqrt(lambda); }
  const num = rx * rx * ry * ry - rx * rx * py * py - ry * ry * px * px;
  const k = (large === sweep ? -1 : 1) * Math.sqrt(Math.max(0, num / (rx * rx * py * py + ry * ry * px * px)));
  const cxp = k * rx * py / ry, cyp = -k * ry * px / rx;
  const cx = cos * cxp - sin * cyp + (x1 + x2) / 2, cy = sin * cxp + cos * cyp + (y1 + y2) / 2;
  const vecAngle = (ux, uy, vx, vy) => Math.atan2(ux * vy - uy * vx, ux * vx + uy * vy);
  const t1 = vecAngle(1, 0, (px - cxp) / rx, (py - cyp) / ry);
  let dt = vecAngle((px - cxp) / rx, (py - cyp) / ry, (-px - cxp) / rx, (-py - cyp) / ry);
  if (!sweep && dt > 0) dt -= 2 * Math.PI;
  if (sweep && dt < 0) dt += 2 * Math.PI;
  const parts = Math.max(1, Math.ceil(Math.abs(dt) / (Math.PI / 2) - 1e-9)), step = dt / parts;
  const alpha = 4 / 3 * Math.tan(step / 4);
  const point = (t) => [cx + rx * Math.cos(t) * cos - ry * Math.sin(t) * sin, cy + rx * Math.cos(t) * sin + ry * Math.sin(t) * cos];
  const deriv = (t) => [-rx * Math.sin(t) * cos - ry * Math.cos(t) * sin, -rx * Math.sin(t) * sin + ry * Math.cos(t) * cos];
  const out = [];
  for (let p = 0, t = t1; p < parts; p++, t += step) {
    const [ax, ay] = point(t), [bx, by] = point(t + step), [dax, day] = deriv(t), [dbx, dby] = deriv(t + step);
    out.push(["C", ax + alpha * dax, ay + alpha * day, bx - alpha * dbx, by - alpha * dby, p === parts - 1 ? x2 : bx, p === parts - 1 ? y2 : by]);
  }
  return out;
}

function ellipse(cx, cy, rx, ry) {
  const kx = rx * KAPPA, ky = ry * KAPPA;
  return [["M", cx + rx, cy], ["C", cx + rx, cy + ky, cx + kx, cy + ry, cx, cy + ry], ["C", cx - kx, cy + ry, cx - rx, cy + ky, cx - rx, cy],
    ["C", cx - rx, cy - ky, cx - kx, cy - ry, cx, cy - ry], ["C", cx + kx, cy - ry, cx + rx, cy - ky, cx + rx, cy], ["Z"]];
}

/** The outline of a basic shape or path element in its own user space, or null. */
function shapeSegments(node, style, viewport) {
  const a = node.attrs, f = style.fontPx;
  const L = (key, of = 0) => length(a[key], f, of);
  switch (node.name) {
    case "path": return parsePath(a.d);
    case "rect": {
      const x = L("x", viewport.w), y = L("y", viewport.h), w = L("width", viewport.w), h = L("height", viewport.h);
      if (!(w > 0 && h > 0)) return null;
      let rx = a.rx !== undefined ? L("rx", viewport.w) : undefined, ry = a.ry !== undefined ? L("ry", viewport.h) : undefined;
      rx = Math.min(rx ?? ry ?? 0, w / 2); ry = Math.min(ry ?? rx, h / 2);
      if (!(rx > 0 && ry > 0)) return [["M", x, y], ["L", x + w, y], ["L", x + w, y + h], ["L", x, y + h], ["Z"]];
      const kx = rx * (1 - KAPPA), ky = ry * (1 - KAPPA);
      return [["M", x + rx, y], ["L", x + w - rx, y], ["C", x + w - kx, y, x + w, y + ky, x + w, y + ry], ["L", x + w, y + h - ry],
        ["C", x + w, y + h - ky, x + w - kx, y + h, x + w - rx, y + h], ["L", x + rx, y + h], ["C", x + kx, y + h, x, y + h - ky, x, y + h - ry],
        ["L", x, y + ry], ["C", x, y + ky, x + kx, y, x + rx, y], ["Z"]];
    }
    case "circle": { const r = L("r"); return r > 0 ? ellipse(L("cx", viewport.w), L("cy", viewport.h), r, r) : null; }
    case "ellipse": { const rx = L("rx"), ry = L("ry"); return rx > 0 && ry > 0 ? ellipse(L("cx", viewport.w), L("cy", viewport.h), rx, ry) : null; }
    case "line": return [["M", L("x1", viewport.w), L("y1", viewport.h)], ["L", L("x2", viewport.w), L("y2", viewport.h)]];
    case "polyline": case "polygon": {
      const n = numbers(a.points);
      if (n.length < 4) return null;
      const out = [["M", n[0], n[1]]];
      for (let i = 2; i + 1 < n.length; i += 2) out.push(["L", n[i], n[i + 1]]);
      if (node.name === "polygon") out.push(["Z"]);
      return out;
    }
    default: return null;
  }
}

// ------------------------------------------------------------------ PDF writing

const fmt = (n) => {
  if (!Number.isFinite(n)) return "0";
  const s = (Math.round(n * 1000) / 1000).toString();
  return s.includes("e") ? n.toFixed(6) : s === "-0" ? "0" : s;
};

function segmentsToOps(segments, matrix) {
  const tx = matrix ? (x, y) => fmt(matrix[0] * x + matrix[2] * y + matrix[4]) + " " + fmt(matrix[1] * x + matrix[3] * y + matrix[5]) : (x, y) => fmt(x) + " " + fmt(y);
  const out = [];
  for (const s of segments) {
    if (s[0] === "M") out.push(tx(s[1], s[2]) + " m");
    else if (s[0] === "L") out.push(tx(s[1], s[2]) + " l");
    else if (s[0] === "C") out.push(tx(s[1], s[2]) + " " + tx(s[3], s[4]) + " " + tx(s[5], s[6]) + " c");
    else out.push("h");
  }
  return out.join("\n");
}

const pdfString = (bytes) => "(" + bytes.replace(/[\\()]/g, "\\$&").replace(/\r/g, "\\r") + ")";

class Writer {
  constructor(ids, images) {
    this.ids = ids;
    this.images = images;  // href -> { name, ... } decoded beforehand
    this.out = [];
    this.gstates = new Map();
    this.usedFonts = new Set();
    this.usedImages = new Set();
    this.depth = 0;
  }

  gstate(fillAlpha, strokeAlpha) {
    const key = `${fmt(fillAlpha)}/${fmt(strokeAlpha)}`;
    if (!this.gstates.has(key)) this.gstates.set(key, { name: `GS${this.gstates.size + 1}`, fill: fillAlpha, stroke: strokeAlpha });
    return this.gstates.get(key).name;
  }

  paint(value, style, opacityKey) {
    if (!value || value === "none") return null;
    let v = String(value).trim();
    const url = v.match(/^url\(\s*['"]?#([^'")]+)['"]?\s*\)\s*(.*)$/);
    if (url) {
      const ref = this.ids.get(url[1]);
      const stops = ref ? ref.children.filter((c) => c.name === "stop") : [];
      if (stops.length) {
        // gradients are drawn with their middle color (figures here use none; this keeps the area visible)
        const stop = stops[Math.floor((stops.length - 1) / 2)];
        const st = computeStyle(stop, { ...DEFAULT_STYLE, fontPx: 16 });
        const color = parseColor(st["stop-color"] === "currentColor" ? style.color : st["stop-color"] || "black");
        if (!color) return null;
        return [color[0], color[1], color[2], color[3] * clamp01(parseFloat(st["stop-opacity"] ?? "1")) * clamp01(parseFloat(style[opacityKey])) * style.alpha];
      }
      v = url[2] || "none";
    }
    if (v === "currentColor") v = style.color;
    const color = parseColor(v);
    if (!color) return null;
    return [color[0], color[1], color[2], color[3] * clamp01(parseFloat(style[opacityKey])) * style.alpha];
  }

  strokeSetup(style, stroke, out) {
    const width = length(style["stroke-width"], style.fontPx);
    if (!(width > 0) || !stroke) return false;
    out.push(`${fmt(width)} w`);
    out.push(`${{ butt: 0, round: 1, square: 2 }[style["stroke-linecap"]] ?? 0} J`);
    out.push(`${{ miter: 0, round: 1, bevel: 2 }[style["stroke-linejoin"]] ?? 0} j`);
    const miter = parseFloat(style["stroke-miterlimit"]);
    if (miter >= 1 && miter !== 10) out.push(`${fmt(miter)} M`);
    const dash = style["stroke-dasharray"] && style["stroke-dasharray"] !== "none" ? numbers(style["stroke-dasharray"]) : [];
    if (dash.length && dash.some((d) => d > 0)) {
      const full = dash.length % 2 ? [...dash, ...dash] : dash;
      out.push(`[${full.map(fmt).join(" ")}] ${fmt(length(style["stroke-dashoffset"], style.fontPx))} d`);
    }
    out.push(`${fmt(stroke[0])} ${fmt(stroke[1])} ${fmt(stroke[2])} RG`);
    return true;
  }

  render(node, parentStyle, viewport) {
    if (node.name === "#text") return;
    switch (node.name) {
      case "defs": case "clipPath": case "linearGradient": case "radialGradient": case "pattern": case "mask": case "marker":
      case "symbol": case "title": case "desc": case "metadata": case "style": case "script": case "filter": case "foreignObject":
        return;
    }
    const style = computeStyle(node, parentStyle);
    if (style.display === "none") return;
    const out = this.out;
    const transform = node.attrs.transform ? parseTransform(node.attrs.transform) : null;
    const clipRef = style["clip-path"] && style["clip-path"].match(/url\(\s*['"]?#([^'")]+)/);
    const clip = clipRef ? this.ids.get(clipRef[1]) : null;
    const save = transform || clip || node.name === "svg" || node.name === "use" || node.name === "image" || node.name === "text";
    if (save) out.push("q");
    if (transform) out.push(`${transform.map(fmt).join(" ")} cm`);
    if (clip) this.clip(clip, style, viewport);

    switch (node.name) {
      case "svg": {
        const a = node.attrs;
        const x = length(a.x, style.fontPx, viewport.w), y = length(a.y, style.fontPx, viewport.h);
        const box = numbers(a.viewBox);
        const w = a.width !== undefined ? length(a.width, style.fontPx, viewport.w) : box[2] ?? viewport.w;
        const h = a.height !== undefined ? length(a.height, style.fontPx, viewport.h) : box[3] ?? viewport.h;
        out.push(`${fmt(x)} ${fmt(y)} ${fmt(w)} ${fmt(h)} re W n`);
        const fit = viewBoxMatrix(box, w, h, a.preserveAspectRatio);
        out.push(`${[1, 0, 0, 1, x, y].map(fmt).join(" ")} cm`);
        if (fit) out.push(`${fit.map(fmt).join(" ")} cm`);
        const inner = box.length === 4 ? { w: box[2], h: box[3] } : { w, h };
        for (const child of node.children) this.render(child, style, inner);
        break;
      }
      case "g": case "a": case "switch":
        for (const child of node.children) this.render(child, style, viewport);
        break;
      case "use": {
        const href = node.attrs.href ?? node.attrs["xlink:href"] ?? "";
        const target = href.startsWith("#") ? this.ids.get(href.slice(1)) : null;
        if (target && this.depth < 20) {
          out.push(`1 0 0 1 ${fmt(length(node.attrs.x, style.fontPx, viewport.w))} ${fmt(length(node.attrs.y, style.fontPx, viewport.h))} cm`);
          this.depth++;
          if (target.name === "symbol") {
            const box = numbers(target.attrs.viewBox);
            const fit = box.length === 4 ? viewBoxMatrix(box, length(node.attrs.width ?? "100%", 16, viewport.w), length(node.attrs.height ?? "100%", 16, viewport.h), target.attrs.preserveAspectRatio) : null;
            if (fit) out.push(`${fit.map(fmt).join(" ")} cm`);
            const symbolStyle = computeStyle(target, style);
            for (const child of target.children) this.render(child, symbolStyle, viewport);
          } else this.render(target, style, viewport);
          this.depth--;
        }
        break;
      }
      case "text": this.text(node, style, viewport); break;
      case "image": this.image(node, style, viewport); break;
      default: {
        const segments = shapeSegments(node, style, viewport);
        if (segments && segments.length && style.visibility !== "hidden" && style.visibility !== "collapse") this.shape(node, segments, style);
      }
    }
    if (save) out.push("Q");
  }

  shape(node, segments, style) {
    const out = this.out;
    const fill = node.name === "line" ? null : this.paint(style.fill, style, "fill-opacity");
    const stroke = this.paint(style.stroke, style, "stroke-opacity");
    const ops = [];
    const stroked = stroke && stroke[3] > 0 && this.strokeSetup(style, stroke, ops);
    const filled = fill && fill[3] > 0;
    if (!stroked && !filled) return;
    out.push("q");
    if (filled) out.push(`${fmt(fill[0])} ${fmt(fill[1])} ${fmt(fill[2])} rg`);
    const fa = filled ? fill[3] : 1, sa = stroked ? stroke[3] : 1;
    if (fa < 1 || sa < 1) out.push(`/${this.gstate(fa, sa)} gs`);
    out.push(...ops);
    out.push(segmentsToOps(segments));
    const evenOdd = style["fill-rule"] === "evenodd";
    out.push(filled && stroked ? (evenOdd ? "B*" : "B") : filled ? (evenOdd ? "f*" : "f") : "S");
    out.push("Q");
  }

  clip(clipNode, style, viewport) {
    const ops = [];
    let evenOdd = false;
    for (const child of clipNode.children) {
      if (child.name === "#text") continue;
      const st = computeStyle(child, style);
      if (st.display === "none") continue;
      const segments = shapeSegments(child, st, viewport);
      if (!segments) continue;
      evenOdd = evenOdd || st["clip-rule"] === "evenodd";
      ops.push(segmentsToOps(segments, child.attrs.transform ? parseTransform(child.attrs.transform) : null));
    }
    const own = clipNode.attrs.transform ? parseTransform(clipNode.attrs.transform) : null;
    if (own) this.out.push(`${own.map(fmt).join(" ")} cm`);
    // an empty clip path hides everything
    this.out.push(ops.length ? ops.join("\n") : "0 0 0 0 re", evenOdd ? "W* n" : "W n");
    if (own) {
      const [a, b, c, d, e, f] = own, det = a * d - b * c;
      if (det) this.out.push(`${[d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det].map(fmt).join(" ")} cm`);
    }
  }

  text(node, style, viewport) {
    if (style.visibility === "hidden") return;
    // lay out the text into chunks (a new chunk starts where x is set) of runs with their own style
    const chunks = [];
    let x = 0, y = 0, chunk = null;
    const xs = (n, st) => (n.attrs.x !== undefined ? String(n.attrs.x).trim().split(/[\s,]+/).map((v) => length(v, st.fontPx, viewport.w)) : null);
    const ys = (n, st) => (n.attrs.y !== undefined ? String(n.attrs.y).trim().split(/[\s,]+/).map((v) => length(v, st.fontPx, viewport.h)) : null);
    const walk = (n, st) => {
      const nx = xs(n, st), ny = ys(n, st);
      if (nx) x = nx[0];
      if (ny) y = ny[0];
      if (n.attrs.dx !== undefined) x += length(String(n.attrs.dx).split(/[\s,]+/)[0], st.fontPx, viewport.w);
      if (n.attrs.dy !== undefined) y += length(String(n.attrs.dy).split(/[\s,]+/)[0], st.fontPx, viewport.h);
      if (nx || !chunk) { chunk = { x, y, anchor: st["text-anchor"], runs: [] }; chunks.push(chunk); }
      for (const child of n.children) {
        if (child.name === "#text") {
          const text = child.text.replace(/[\s​]+/g, " ");
          if (!text) continue;
          const bold = /^(bold|bolder|[6-9]00)$/i.test(String(st["font-weight"]).trim());
          const italic = /italic|oblique/i.test(st["font-style"]);
          const shift = baselineShift(st, n === node ? style : st);
          const run = { text, x, y: y + shift, style: st, size: st.fontPx, bold, italic };
          run.width = textWidth(text, st.fontPx, bold);
          chunk.runs.push(run);
          x += run.width;
        } else if (child.name === "tspan" || child.name === "a") {
          const cst = computeStyle(child, st);
          if (cst.display !== "none") walk(child, cst);
        }
      }
    };
    walk(node, style);
    // collapse the spaces at the start and the end of the text, as SVG's default white-space handling does
    const runs = chunks.flatMap((c) => c.runs);
    if (runs.length) {
      const first = runs[0], last = runs[runs.length - 1];
      const lead = first.text.match(/^ */)[0].length;
      if (lead) { first.text = first.text.slice(lead); const w = textWidth(" ", first.size, first.bold) * lead; first.width -= w; for (const c of chunks) for (const r of c.runs) if (r !== first && c === chunks[0]) r.x -= w; }
      last.text = last.text.replace(/ +$/, "");
      last.width = textWidth(last.text, last.size, last.bold);
    }
    const out = this.out;
    for (const c of chunks) {
      const total = c.runs.reduce((sum, r) => sum + r.width, 0);
      const offset = c.anchor === "middle" ? -total / 2 : c.anchor === "end" ? -total : 0;
      for (const run of c.runs) {
        if (!run.text) continue;
        const st = run.style;
        const fill = this.paint(st.fill, st, "fill-opacity");
        const stroke = this.paint(st.stroke, st, "stroke-opacity");
        const strokeOps = [];
        const stroked = stroke && stroke[3] > 0 && this.strokeSetup(st, stroke, strokeOps);
        const filled = fill && fill[3] > 0;
        if (!filled && !stroked) continue;
        const draw = (mode) => {
          out.push("BT", `${mode} Tr`);
          let at = run.x + offset;
          for (const piece of textRuns(run.text, run.bold, run.italic)) {
            const font = piece.font === "Sym" ? "Sym" : piece.font;
            this.usedFonts.add(font);
            const size = piece.script ? run.size * SCRIPT_SCALE : run.size;
            const rise = piece.script === "sup" ? -0.36 * run.size : piece.script === "sub" ? 0.14 * run.size : 0;
            out.push(`/${font} ${fmt(size)} Tf`, `1 0 0 -1 ${fmt(at)} ${fmt(run.y + rise)} Tm`, `${pdfString(piece.bytes)} Tj`);
            at += piece.width * run.size / 1000;
          }
          out.push("ET");
        };
        out.push("q");
        if (filled) out.push(`${fmt(fill[0])} ${fmt(fill[1])} ${fmt(fill[2])} rg`);
        const fa = filled ? fill[3] : 1, sa = stroked ? stroke[3] : 1;
        if (fa < 1 || sa < 1) out.push(`/${this.gstate(fa, sa)} gs`);
        out.push(...strokeOps);
        if (stroked && filled && /^\s*stroke/.test(st["paint-order"])) { draw(1); draw(0); }  // halo under the letters
        else draw(stroked && filled ? 2 : stroked ? 1 : 0);
        out.push("Q");
      }
    }
  }

  image(node, style, viewport) {
    const a = node.attrs;
    const href = a.href ?? a["xlink:href"];
    const image = href && this.images.get(href);
    if (!image || style.visibility === "hidden") return;
    const x = length(a.x, style.fontPx, viewport.w), y = length(a.y, style.fontPx, viewport.h);
    const w = a.width !== undefined ? length(a.width, style.fontPx, viewport.w) : image.width;
    const h = a.height !== undefined ? length(a.height, style.fontPx, viewport.h) : image.height;
    if (!(w > 0 && h > 0)) return;
    const out = this.out;
    out.push(`${fmt(x)} ${fmt(y)} ${fmt(w)} ${fmt(h)} re W n`);
    const fit = viewBoxMatrix([0, 0, image.width, image.height], w, h, a.preserveAspectRatio) || [w / image.width, 0, 0, h / image.height, 0, 0];
    out.push(`1 0 0 1 ${fmt(x)} ${fmt(y)} cm`, `${fit.map(fmt).join(" ")} cm`);
    if (style.alpha < 1) out.push(`/${this.gstate(style.alpha, style.alpha)} gs`);
    // the image's unit square, its first row at the top
    out.push(`${fmt(image.width)} 0 0 ${fmt(-image.height)} 0 ${fmt(image.height)} cm`, `/${image.name} Do`);
    image.smooth = image.smooth ?? !/pixelated|optimizespeed|crisp-edges/i.test(style["image-rendering"]);
    this.usedImages.add(image);
  }
}

/** The matrix that maps a viewBox onto a w x h viewport (SVG's preserveAspectRatio), or null without a viewBox. */
function viewBoxMatrix(box, w, h, preserve = "xMidYMid meet") {
  if (box.length !== 4 || !(box[2] > 0 && box[3] > 0)) return null;
  const [align = "xMidYMid", mode = "meet"] = String(preserve || "xMidYMid meet").trim().split(/\s+/);
  let sx = w / box[2], sy = h / box[3];
  if (align !== "none") { sx = sy = mode === "slice" ? Math.max(sx, sy) : Math.min(sx, sy); }
  let tx = -box[0] * sx, ty = -box[1] * sy;
  if (align !== "none") {
    const fx = /xMid/.test(align) ? 0.5 : /xMax/.test(align) ? 1 : 0, fy = /YMid/.test(align) ? 0.5 : /YMax/.test(align) ? 1 : 0;
    tx += (w - box[2] * sx) * fx; ty += (h - box[3] * sy) * fy;
  }
  return [sx, 0, 0, sy, tx, ty];
}

/** How far below the alphabetic baseline (px) the text's y is, from dominant-baseline / alignment-baseline. */
function baselineShift(style) {
  const size = style.fontPx;
  const which = (style["alignment-baseline"] && style["alignment-baseline"] !== "auto" && style["alignment-baseline"] !== "baseline")
    ? style["alignment-baseline"] : style["dominant-baseline"];
  let shift = 0;
  switch (String(which || "auto").trim()) {
    case "middle": shift = 0.26 * size; break;               // half Arial's x-height above the baseline
    case "central": case "mathematical": shift = 0.35 * size; break;
    case "hanging": shift = 0.72 * size; break;
    case "text-before-edge": case "before-edge": case "text-top": case "top": shift = 0.905 * size; break;
    case "text-after-edge": case "after-edge": case "ideographic": case "text-bottom": case "bottom": shift = -0.212 * size; break;
  }
  const bs = style["baseline-shift"];
  if (bs === "super") shift -= 0.33 * size;
  else if (bs === "sub") shift += 0.2 * size;
  else if (bs && bs !== "baseline") shift -= length(bs, size, size);
  return shift;
}

async function deflate(bytes) {
  if (typeof CompressionStream !== "function") return null;
  try {
    const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  } catch {
    return null;
  }
}

const latin1 = (text) => {
  const out = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i) & 255;
  return out;
};

/** Decodes an image URL to RGBA pixels in a browser. */
export async function browserDecodeImage(href) {
  const img = new Image();
  img.crossOrigin = "anonymous";
  img.src = href;
  await img.decode();
  const canvas = document.createElement("canvas");
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  const context = canvas.getContext("2d");
  context.drawImage(img, 0, 0);
  return { width: canvas.width, height: canvas.height, rgba: context.getImageData(0, 0, canvas.width, canvas.height).data };
}

/** The root's size in px, from width / height or the viewBox. */
export function svgSize(svgOrText) {
  const svg = typeof svgOrText === "string" ? parseSvg(svgOrText).svg : svgOrText;
  const box = numbers(svg.attrs.viewBox);
  const width = length(svg.attrs.width, 16, box[2] || 0) || box[2] || 300;
  const height = length(svg.attrs.height, 16, box[3] || 0) || box[3] || 150;
  return { width, height };
}

/**
 * Converts SVG text to a one-page vector PDF (a Blob). `decodeImage(href)` -> { width, height, rgba } reads embedded
 * raster images (the browser's own decoder by default); images it cannot read are left out.
 */
export async function svgToPdf(svgText, { decodeImage = typeof document !== "undefined" ? browserDecodeImage : null, title = "" } = {}) {
  const { svg, ids } = parseSvg(svgText);
  const { width, height } = svgSize(svg);

  // read the embedded images first; drawing is synchronous
  const images = new Map();
  const hrefs = new Set();
  const collect = (node) => {
    if (node.name === "image") { const href = node.attrs.href ?? node.attrs["xlink:href"]; if (href) hrefs.add(href); }
    for (const child of node.children || []) collect(child);
  };
  collect(svg);
  for (const href of hrefs) {
    if (!decodeImage) break;
    try {
      const image = await decodeImage(href);
      if (image && image.width > 0 && image.height > 0) images.set(href, { ...image, name: `Im${images.size + 1}` });
    } catch {
      // an unreadable image is left out
    }
  }

  const writer = new Writer(ids, images);
  const k = 0.75;  // CSS px -> pt
  writer.out.push(`${k} 0 0 ${-k} 0 ${fmt(height * k)} cm`);
  const rootStyle = { ...DEFAULT_STYLE, fontPx: 16 };
  // the root element: its viewBox maps onto width x height; its own size is the page
  const box = numbers(svg.attrs.viewBox);
  const fit = viewBoxMatrix(box, width, height, svg.attrs.preserveAspectRatio);
  const style = computeStyle(svg, rootStyle);
  if (svg.attrs.transform) writer.out.push(`${parseTransform(svg.attrs.transform).map(fmt).join(" ")} cm`);
  if (fit) writer.out.push(`${fit.map(fmt).join(" ")} cm`);
  const viewport = box.length === 4 ? { w: box[2], h: box[3] } : { w: width, h: height };
  for (const child of svg.children) writer.render(child, style, viewport);

  // objects: 1 catalog, 2 pages, 3 page, 4 contents, 5 info, then fonts, graphics states and images
  const objects = [];
  const add = (body) => { objects.push(body); return objects.length; };
  const content = latin1(writer.out.join("\n"));
  const contentZ = await deflate(content);
  add(null); add(null); add(null);
  const contentsId = add({ dict: contentZ ? "/Filter /FlateDecode" : "", data: contentZ || content });
  const now = new Date(), pad = (n) => String(n).padStart(2, "0");
  const date = `D:${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}Z`;
  const infoId = add(`<< /Producer (Omics Studio) /CreationDate (${date})${title ? ` /Title ${pdfString(textRuns(title).map((r) => r.bytes).join(""))}` : ""} >>`);

  const fontRefs = [];
  for (const key of ["F1", "F2", "F3", "F4"]) {
    if (!writer.usedFonts.has(key)) continue;
    const f = FONTS[key];
    const descriptor = add(`<< /Type /FontDescriptor /FontName /${f.name} /Flags ${f.flags} /FontBBox [${f.bbox}] /ItalicAngle ${f.italic} /Ascent 905 /Descent -212 /CapHeight 716 /StemV ${f.stem} >>`);
    const font = add(`<< /Type /Font /Subtype /TrueType /BaseFont /${f.name} /FirstChar 32 /LastChar 255 /Widths [${f.widths.join(" ")}] /Encoding /WinAnsiEncoding /FontDescriptor ${descriptor} 0 R >>`);
    fontRefs.push(`/${key} ${font} 0 R`);
  }
  if (writer.usedFonts.has("Sym")) fontRefs.push(`/Sym ${add("<< /Type /Font /Subtype /Type1 /BaseFont /Symbol >>")} 0 R`);

  const gsRefs = [...writer.gstates.values()].map((g) => `/${g.name} ${add(`<< /Type /ExtGState /ca ${fmt(g.fill)} /CA ${fmt(g.stroke)} >>`)} 0 R`);

  const imageRefs = [];
  for (const image of writer.usedImages) {
    const n = image.width * image.height, rgb = new Uint8Array(n * 3), alpha = new Uint8Array(n);
    let translucent = false;
    for (let i = 0, j = 0; i < n; i++, j += 4) {
      rgb[i * 3] = image.rgba[j]; rgb[i * 3 + 1] = image.rgba[j + 1]; rgb[i * 3 + 2] = image.rgba[j + 2];
      alpha[i] = image.rgba[j + 3];
      if (alpha[i] !== 255) translucent = true;
    }
    const common = `/Type /XObject /Subtype /Image /Width ${image.width} /Height ${image.height} /BitsPerComponent 8${image.smooth ? " /Interpolate true" : ""}`;
    let smask = "";
    if (translucent) {
      const z = await deflate(alpha);
      smask = ` /SMask ${add({ dict: `${common} /ColorSpace /DeviceGray${z ? " /Filter /FlateDecode" : ""}`, data: z || alpha })} 0 R`;
    }
    const z = await deflate(rgb);
    imageRefs.push(`/${image.name} ${add({ dict: `${common} /ColorSpace /DeviceRGB${smask}${z ? " /Filter /FlateDecode" : ""}`, data: z || rgb })} 0 R`);
  }

  const resources = [
    fontRefs.length ? `/Font << ${fontRefs.join(" ")} >>` : "",
    gsRefs.length ? `/ExtGState << ${gsRefs.join(" ")} >>` : "",
    imageRefs.length ? `/XObject << ${imageRefs.join(" ")} >>` : "",
  ].filter(Boolean).join(" ");
  objects[0] = "<< /Type /Catalog /Pages 2 0 R >>";
  objects[1] = "<< /Type /Pages /Kids [3 0 R] /Count 1 >>";
  const transparent = gsRefs.length || imageRefs.length ? " /Group << /S /Transparency /CS /DeviceRGB >>" : "";
  objects[2] = `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${fmt(width * k)} ${fmt(height * k)}] /Resources << ${resources} >> /Contents ${contentsId} 0 R${transparent} >>`;

  const chunks = [latin1("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")];
  let offset = chunks[0].length;
  const offsets = [];
  for (let i = 0; i < objects.length; i++) {
    offsets.push(offset);
    const body = objects[i];
    const parts = typeof body === "string"
      ? [latin1(`${i + 1} 0 obj\n${body}\nendobj\n`)]
      : [latin1(`${i + 1} 0 obj\n<< ${body.dict} /Length ${body.data.length} >>\nstream\n`), body.data, latin1("\nendstream\nendobj\n")];
    for (const part of parts) { chunks.push(part); offset += part.length; }
  }
  const xref = [`xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`, ...offsets.map((o) => `${String(o).padStart(10, "0")} 00000 n \n`)].join("");
  chunks.push(latin1(`${xref}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R /Info ${infoId} 0 R >>\nstartxref\n${offset}\n%%EOF\n`));
  return new Blob(chunks, { type: "application/pdf" });
}

// ------------------------------------------------------------------ PNG and saving

/** Renders SVG text to a PNG Blob at `scale` (capped so neither side exceeds `max` pixels). */
export async function svgToPng(svgText, { scale = 2, max = 8192 } = {}) {
  const { width, height } = svgSize(svgText);
  const factor = Math.min(scale, max / Math.max(width, height));
  const url = URL.createObjectURL(new Blob([svgText], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const image = new Image();
    image.decoding = "async";
    image.src = url;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * factor));
    canvas.height = Math.max(1, Math.round(height * factor));
    canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
    return await new Promise((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("PNG encoder returned no data"))), "image/png"));
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** The figure in one format: "svg", "png" or "pdf". */
export async function figureBlob(svgText, format, options = {}) {
  if (format === "svg") return new Blob([svgText], { type: "image/svg+xml;charset=utf-8" });
  if (format === "pdf") return svgToPdf(svgText, options);
  return svgToPng(svgText, options);
}

/** Saves a Blob as a download (the Mac apps show their save panel for it). */
export function saveBlob(blob, name) {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 10000);
}

export const FIGURE_FORMATS = ["png", "svg", "pdf"];
