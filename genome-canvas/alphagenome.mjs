// AlphaGenome in Genome Canvas: the track library's AlphaGenome card (start / stop the model on the GPU server, choose the
// backend, cell type and outputs, predict the current view) and the stage's "AG predict" button (an rsID: jump to a
// window around it and predict REF, ALT and ALT − REF). Predictions come back as in-memory signal tracks ("AG · …").
//
// Everything goes through Omics Studio's AlphaGenome module on the same origin (/alphagenome/, alphagenome/server.py):
// studio-api/status|start|stop to run the model, and AlphaGenome Atlas's API (api/biosamples, api/variant-search,
// api/predict) for predictions. Without that module (This Mac mode, a stand-alone Genome Canvas) the card stays hidden
// and the button disabled. GRCh38 only.

const AG = "/alphagenome/";
const KEY = "genome-canvas:alphagenome";
const MAX_WINDOW = 524_288;          // AlphaGenome Atlas's limit (deploy/proxy.py MAX_INTERVAL_BP)
const AG2_WINDOWS = [16_384, 131_072, 524_288];
const DEFAULT_BIOSAMPLE = { ag1: "CL:0002592", ag2: "UBERON:0002257_CL:0002592" };
const REF_COLOR = "#8a93a6";         // REF under ALT in a variant's overlay (ALT keeps the output's own colour)
const DELTA_NEGATIVE = "#bf616a";    // ALT − REF below zero

const $ = (selector) => document.querySelector(selector);

function stored() {
  try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch { return {}; }
}
function store(patch) {
  try { localStorage.setItem(KEY, JSON.stringify({ ...stored(), ...patch })); } catch { /* storage disabled */ }
}

async function getJSON(path, options) {
  const response = await fetch(AG + path, { cache: "no-store", ...options });
  let body = null;
  try { body = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) throw new Error(body?.error || `${response.status} ${response.statusText}`);
  return body;
}

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

/** Bins of a prediction → IGV wig features over [start, end). */
export function binsToFeatures(chromosome, start, end, values) {
  const n = values.length;
  const step = (end - start) / n;
  const features = new Array(n);
  for (let i = 0; i < n; i += 1) {
    const s = Math.round(start + i * step);
    features[i] = { chr: chromosome, start: s, end: Math.max(s + 1, Math.round(start + (i + 1) * step)), value: Number(values[i]) || 0 };
  }
  return features;
}

/** The model window for a view: centred, a multiple of 2,048 bp (AG2: one of its three lengths), at most 524,288 bp. */
export function modelWindow(chromosome, start, end, backend = "ag1") {
  const width = Math.max(1, end - start);
  let length;
  if (backend === "ag2") length = AG2_WINDOWS.find((w) => w >= width) || AG2_WINDOWS[AG2_WINDOWS.length - 1];
  else length = Math.min(MAX_WINDOW, Math.max(2048, Math.ceil(width / 2048) * 2048));
  const centre = Math.round((start + end) / 2);
  const windowStart = Math.max(0, centre - Math.floor(length / 2));
  return { chromosome, start: windowStart, end: windowStart + length };
}

/**
 * ctx: { browser(): igv browser, genome(): genome id, loadTrack(config), toast(text, kind), setStatus(text, kind),
 *        highlight(chr, start, end, color), search(locus) }
 */
export function installAlphaGenome(ctx) {
  const ui = {
    toggle: $("#ag-button"), card: $("#ag-card"), power: $("#ag-power"), state: $("#ag-state"), body: $("#ag-body"),
    backends: [...document.querySelectorAll("[data-ag-backend]")], filter: $("#ag-biosample-filter"),
    biosample: $("#ag-biosample"), tracks: $("#ag-tracks"), predictView: $("#ag-predict-view"), note: $("#ag-note"),
    stageButton: $("#ag-predict-button"), dialog: $("#ag-dialog"), rsid: $("#ag-rsid"), alleles: $("#ag-alleles"),
    go: $("#ag-rsid-go"), dialogStatus: $("#ag-dialog-status"), dialogTracks: $("#ag-dialog-tracks"),
  };
  if (!ui.card || !ui.toggle) return;
  const saved = stored();
  const s = {
    available: false, phase: "unknown", status: null, backend: saved.backend === "ag2" ? "ag2" : "ag1",
    catalogs: {}, selected: saved.tracks || {}, biosample: saved.biosample || {}, busy: false, loaded: [],
    watchedUntil: 0, resolved: null, expanded: false,
  };

  // ---------------------------------------------------------------- the model's state
  async function refresh() {
    try {
      const status = await getJSON("studio-api/status");
      s.available = true;
      s.status = status;
      s.phase = status.phase === "ready" && !status.tunnel ? "starting" : status.phase;
    } catch {
      // not behind Omics Studio (or its AlphaGenome module is down): only its health tells which
      try { s.available = (await fetch(AG + "health", { cache: "no-store" })).ok; } catch { s.available = false; }
      s.phase = "unknown";
    }
    render();
    if (s.phase === "ready" && !s.catalogs[s.backend]) loadCatalog().catch((error) => note(error.message, true));
  }

  function schedule() {
    const fast = s.phase === "starting" || s.phase === "stopping" || Date.now() < s.watchedUntil;
    window.setTimeout(() => refresh().finally(schedule), fast ? 4000 : 30000);
  }

  const LABEL = { ready: "Running", starting: "Starting… (1–4 min)", stopping: "Stopping…", stopped: "Off · GPUs free", unknown: "Unreachable" };

  function render() {
    // the button beside GREAT; its panel opens below it on a click (closed when the page opens)
    ui.toggle.hidden = !s.available;
    ui.toggle.dataset.phase = s.phase;
    ui.toggle.setAttribute("aria-expanded", String(s.expanded && s.available));
    ui.toggle.title = `AlphaGenome · ${LABEL[s.phase] || s.phase}: start / stop the model, choose cell type and outputs, predict this view`;
    ui.card.hidden = !(s.available && s.expanded);
    const ready = s.phase === "ready";
    const hg38 = /^hg38$/i.test(ctx.genome() || "");
    ui.power.setAttribute("aria-checked", String(ready || s.phase === "starting"));
    ui.power.disabled = s.busy || s.phase === "stopping" || s.phase === "unknown";
    ui.card.dataset.phase = s.phase;
    ui.state.textContent = LABEL[s.phase] || s.phase;
    ui.state.title = s.status?.startedBy ? `Started by ${s.status.startedBy}` : "";
    ui.body.hidden = !(ready || s.phase === "starting");
    for (const b of ui.backends) b.setAttribute("aria-pressed", String(b.dataset.agBackend === s.backend));
    const can = ready && hg38 && !s.busy && selectedTracks().length > 0;
    ui.predictView.disabled = !can;
    ui.stageButton.disabled = !(ready && hg38);
    ui.stageButton.title = !s.available ? "AlphaGenome is not available here"
      : !ready ? "Start AlphaGenome first: the AlphaGenome button in the track library, beside GREAT"
        : !hg38 ? "AlphaGenome predicts on GRCh38 (hg38) only"
          : "AlphaGenome: an rsID's effect (REF, ALT, ALT − REF) on the chosen tracks";
    if (!hg38 && ready) note("AlphaGenome predicts on GRCh38: switch the genome to hg38.", true);
  }

  function note(text, error = false) {
    ui.note.textContent = text || "";
    ui.note.classList.toggle("error", Boolean(error));
  }

  ui.toggle.addEventListener("click", () => {
    s.expanded = !s.expanded;
    if (s.expanded) s.watchedUntil = Date.now() + 2 * 60_000;
    render();
    if (s.expanded) refresh();
  });

  ui.power.addEventListener("click", async () => {
    const on = ui.power.getAttribute("aria-checked") === "true";
    if (on && !window.confirm("Stop AlphaGenome and free the GPUs? Others using it lose it too.")) return;
    s.busy = true;
    s.watchedUntil = Date.now() + 10 * 60_000;
    render();
    try {
      const result = await getJSON(on ? "studio-api/stop" : "studio-api/start", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      if (result && result.ok === false) throw new Error(result.error || "refused");
      ctx.toast(on ? "AlphaGenome stopped: the GPUs are free" : "Starting AlphaGenome on the GPU server (1–4 minutes)…");
    } catch (error) {
      ctx.toast(`AlphaGenome: ${error.message}`, "error");
    } finally {
      s.busy = false;
      await refresh();
    }
  });

  // ---------------------------------------------------------------- cell types and outputs
  async function loadCatalog() {
    const backend = s.backend;
    if (!s.catalogs[backend]) {
      ui.biosample.innerHTML = "<option>Loading cell types…</option>";
      const body = await getJSON(`api/biosamples?backend=${backend}`);
      s.catalogs[backend] = (body.biosamples || []).filter((b) => (b.model_tracks || []).length);
    }
    fillBiosamples();
  }

  function catalog() { return s.catalogs[s.backend] || []; }
  function currentBiosample() {
    const id = s.biosample[s.backend] || DEFAULT_BIOSAMPLE[s.backend];
    return catalog().find((b) => b.curie === id) || catalog().find((b) => b.curie.includes("CL:0002592")) || catalog()[0];
  }

  function fillBiosamples() {
    const query = ui.filter.value.trim().toLowerCase();
    const current = currentBiosample();
    const list = catalog().filter((b) => !query || b === current || `${b.label} ${b.curie}`.toLowerCase().includes(query));
    ui.biosample.innerHTML = list.slice(0, 400).map((b) =>
      `<option value="${escapeHTML(b.curie)}"${b === current ? " selected" : ""}>${escapeHTML(b.label)} · ${escapeHTML(b.curie)}</option>`).join("");
    fillTracks();
  }

  function selectedTracks() {
    const b = currentBiosample();
    if (!b) return [];
    const chosen = s.selected[`${s.backend}|${b.curie}`];
    return (b.model_tracks || []).filter((t) => (chosen ? chosen.includes(t.id) : defaultTrack(t)));
  }
  // default: the accessibility and histone outputs (RNA-seq on request: it doubles the prediction's work)
  const defaultTrack = (t) => t.output !== "RNA_SEQ";

  function fillTracks() {
    const b = currentBiosample();
    const picked = new Set(selectedTracks().map((t) => t.id));
    const rows = (b?.model_tracks || []).map((t) => `
      <label class="ag-track" title="${escapeHTML(t.detail || t.label)}">
        <input type="checkbox" value="${escapeHTML(t.id)}"${picked.has(t.id) ? " checked" : ""}>
        <i style="background:${escapeHTML(t.color || "#4c7a86")}"></i><span>${escapeHTML(t.label)}</span>
      </label>`).join("");
    ui.tracks.innerHTML = rows || '<p class="ag-empty">No model outputs for this cell type.</p>';
    if (ui.dialogTracks) ui.dialogTracks.textContent = selectedTracks().map((t) => t.label).join(", ") || "none: choose them in the AlphaGenome card";
    render();
  }

  ui.backends.forEach((button) => button.addEventListener("click", () => {
    s.backend = button.dataset.agBackend;
    store({ backend: s.backend });
    render();
    loadCatalog().catch((error) => note(error.message, true));
  }));
  ui.filter.addEventListener("input", fillBiosamples);
  ui.biosample.addEventListener("change", () => {
    s.biosample[s.backend] = ui.biosample.value;
    store({ biosample: s.biosample });
    fillTracks();
  });
  ui.tracks.addEventListener("change", () => {
    const b = currentBiosample();
    s.selected[`${s.backend}|${b.curie}`] = [...ui.tracks.querySelectorAll("input:checked")].map((i) => i.value);
    store({ tracks: s.selected });
    fillTracks();
  });

  // ---------------------------------------------------------------- predictions
  function view() {
    const frame = ctx.browser()?.referenceFrameList?.[0];
    if (!frame) return null;
    const start = Math.floor(frame.start);
    const end = Math.ceil(frame.end ?? (frame.start + frame.bpPerPixel * (frame.widthInPixels || 1000)));
    return { chromosome: frame.chr, start, end };
  }

  function bins(length) {
    const px = ctx.browser()?.referenceFrameList?.[0]?.widthInPixels || 1200;
    return Math.max(200, Math.min(4000, length, Math.round(px * 1.5)));
  }

  async function predict(interval, variant = null) {
    const tracks = selectedTracks();
    if (!tracks.length) throw new Error("Choose at least one output in the AlphaGenome card");
    const body = {
      backend: s.backend, interval, organism: "HOMO_SAPIENS", bins: bins(interval.end - interval.start),
      tracks: tracks.map(({ id, label, output, ontology, column, model_id: modelId }) => ({ id, label, output, ontology, column, model_id: modelId })),
      variant,
    };
    const result = await getJSON("api/predict", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    return { result, tracks };
  }

  function removeLoaded() {
    const browser = ctx.browser();
    for (const track of s.loaded) {
      try { browser.removeTrack(track); } catch { /* already removed by hand */ }
    }
    s.loaded = [];
  }

  async function show(result, tracks, variant, rsid) {
    removeLoaded();
    const biosample = currentBiosample();
    const colours = Object.fromEntries(tracks.map((t) => [t.id, t.color || "#4c7a86"]));
    for (const t of result.tracks || []) {
      const iv = t.interval || result.interval;
      const colour = colours[t.id] || "#4c7a86";
      const base = { genomeCanvasAlphaGenome: true, autoscale: true, height: 70, color: colour };
      const tip = `${t.label} · ${biosample?.label || ""} · ${s.backend.toUpperCase()}`;
      if (variant && t.alternate) {
        const name = `AG · ${rsid || "variant"} · ${t.label}`;
        s.loaded.push(await ctx.loadTrack({
          type: "merged", name, genomeCanvasAlphaGenome: true, height: 80, autoscale: true,
          description: `${tip}: REF (grey) and ALT`,
          tracks: [
            { ...base, type: "wig", name: "REF", color: REF_COLOR, features: binsToFeatures(iv.chromosome, iv.start, iv.end, t.reference) },
            { ...base, type: "wig", name: "ALT", features: binsToFeatures(iv.chromosome, iv.start, iv.end, t.alternate) },
          ],
        }));
        s.loaded.push(await ctx.loadTrack({
          ...base, type: "wig", name: `AG Δ · ${rsid || "variant"} · ${t.label}`, altColor: DELTA_NEGATIVE, height: 60,
          description: `${tip}: ALT − REF (max |Δ| ${Number(t.summary?.max_abs || 0).toPrecision(3)})`,
          features: binsToFeatures(iv.chromosome, iv.start, iv.end, t.delta),
        }));
      } else {
        s.loaded.push(await ctx.loadTrack({
          ...base, type: "wig", name: `AG · ${t.label}`, description: tip,
          features: binsToFeatures(iv.chromosome, iv.start, iv.end, t.values || t.reference || []),
        }));
      }
    }
    s.loaded = s.loaded.filter(Boolean);
  }

  ui.predictView.addEventListener("click", async () => {
    const v = view();
    if (!v) return;
    if (v.end - v.start > MAX_WINDOW) {
      note(`Zoom in: AlphaGenome predicts at most ${MAX_WINDOW.toLocaleString()} bp (the view is ${(v.end - v.start).toLocaleString()} bp).`, true);
      return;
    }
    const interval = modelWindow(v.chromosome, v.start, v.end, s.backend);
    s.busy = true;
    render();
    note(`Predicting ${interval.chromosome}:${interval.start.toLocaleString()}-${interval.end.toLocaleString()}…`);
    ctx.setStatus("AlphaGenome: predicting…", "busy");
    const started = performance.now();
    try {
      const { result, tracks } = await predict(interval);
      await show(result, tracks, null, null);
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      note(`${tracks.length} output${tracks.length === 1 ? "" : "s"} · ${(interval.end - interval.start).toLocaleString()} bp · ${seconds} s`);
      ctx.setStatus("Ready");
    } catch (error) {
      note(error.message, true);
      ctx.setStatus("AlphaGenome prediction failed", "error");
    } finally {
      s.busy = false;
      render();
    }
  });

  // ---- the stage's "AG predict": an rsID
  ui.stageButton.addEventListener("click", () => {
    ui.dialogStatus.textContent = "";
    ui.alleles.closest("label").hidden = true;
    s.resolved = null;
    fillTracks();
    ui.dialog.showModal();
    ui.rsid.focus();
    ui.rsid.select();
  });

  ui.rsid.addEventListener("input", () => { s.resolved = null; ui.alleles.closest("label").hidden = true; });

  ui.go.addEventListener("click", async (event) => {
    event.preventDefault();
    const rsid = ui.rsid.value.trim();
    if (!/^rs[1-9]\d*$/i.test(rsid)) {
      ui.dialogStatus.textContent = "Enter an rsID such as rs429358.";
      return;
    }
    ui.go.disabled = true;
    try {
      if (!s.resolved || s.resolved.requested_rsid?.toLowerCase() !== rsid.toLowerCase()) {
        ui.dialogStatus.textContent = `Looking up ${rsid} in dbSNP…`;
        s.resolved = await getJSON(`api/variant-search?${new URLSearchParams({ q: rsid })}`);
        const variants = s.resolved.variants || [];
        if (!variants.length) throw new Error(`No GRCh38 alleles found for ${rsid}`);
        ui.alleles.innerHTML = variants.map((v, i) =>
          `<option value="${i}">${escapeHTML(v.reference_bases)} → ${escapeHTML(v.alternate_bases)}</option>`).join("");
        ui.alleles.closest("label").hidden = variants.length < 2;
        if (variants.length > 1) {
          ui.dialogStatus.textContent = `${rsid} has ${variants.length} ALT alleles: choose one, then Predict.`;
          return;
        }
      }
      const variant = s.resolved.variants[Number(ui.alleles.value || 0)];
      const rsName = s.resolved.rsid || rsid;
      // the window AlphaGenome Atlas uses around a variant (131,072 bp), on AG2 one of its lengths
      const region = modelWindow(variant.chromosome, variant.position - 65_536, variant.position + 65_536, s.backend);
      ui.dialogStatus.textContent = `${rsName} · ${variant.chromosome}:${variant.position.toLocaleString()} ${variant.reference_bases}>${variant.alternate_bases} · predicting…`;
      await ctx.search(`${region.chromosome}:${region.start + 1}-${region.end}`);
      ctx.highlight(variant.chromosome, variant.position - 1, variant.position - 1 + Math.max(1, variant.reference_bases.length));
      ctx.setStatus(`AlphaGenome: ${rsName}…`, "busy");
      const started = performance.now();
      const { result, tracks } = await predict(region, variant);
      await show(result, tracks, variant, rsName);
      ui.dialog.close();
      const top = (result.tracks || []).map((t) => `${t.label} ${Number(t.summary?.max_abs || 0).toPrecision(2)}`).join(" · ");
      ctx.toast(`${rsName} ${variant.reference_bases}>${variant.alternate_bases}: max |ALT − REF| ${top} (${((performance.now() - started) / 1000).toFixed(1)} s)`);
      ctx.setStatus("Ready");
    } catch (error) {
      ui.dialogStatus.textContent = error.message;
      ctx.setStatus("AlphaGenome prediction failed", "error");
    } finally {
      ui.go.disabled = false;
    }
  });

  refresh().finally(schedule);
  return { refresh, render, isAlphaGenomeTrack: (config) => Boolean(config?.genomeCanvasAlphaGenome) };
}
