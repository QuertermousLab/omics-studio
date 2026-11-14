// Single cell: Cell Ranger (GEX, ATAC, multiome) with cellranger.sh, then the Seurat / Signac analysis.

import { $, $$, RunPanel, bindBrowse, esc, fmtBytes, pathField, showScript, store, toast, wb } from './common';

interface Lib {
  id: string; key: string; prefix: string; name: string; dirs: string[]; parts: { dir: string; prefix: string }[]; reads: string[]; size: number;
  files: number; kind: string; merged: boolean; runs: number; include?: boolean;
}
interface Pair { gex: string; atac: string; name: string; include?: boolean }
interface Run { kind: string; outs: string; name: string; aggr: boolean; include?: boolean }

const MODES: [string, string, string][] = [
  ['gex', 'Gene expression', 'cellranger count (scRNA-seq, snRNA-seq)'],
  ['atac', 'ATAC', 'cellranger-atac count (scATAC-seq)'],
  ['multiome', 'Multiome', 'cellranger-arc count: GEX + ATAC of the same nuclei'],
  ['seurat', 'Seurat only', 'Analyse Cell Ranger outputs that already exist'],
];

export function singleCellPage(root: HTMLElement) {
  let mode = store.get('sc:mode', 'gex');
  let libs: Lib[] = [], pairs: Pair[] = [], runs: Run[] = [];
  let refColumns: { name: string; levels: number; examples: string[] }[] = [];
  root.innerHTML = `<header class="wb-head"><h1>Single cell (10x Genomics)</h1>
      <p class="muted">Cell Ranger for each sample, optionally aggregated, then the Seurat / Signac analysis: QC with per-sample thresholds (median ± MAD,
      proposed automatically for you to review), clustering, UMAP, label transfer from a reference, RDS and h5ad / h5mu.</p></header>
    <section class="card wb-card"><h3>1 · Data</h3><div class="wb-seg" data-modes>${MODES.map(([id, label]) => `<button type="button" data-mode="${id}">${label}</button>`).join('')}</div>
      <p class="muted small" data-mode-note></p>
      <div class="wb-row" data-fastq-row>${pathField('fastq', 'FASTQ folder', store.get('sc:fastq'))}<div data-atac-folder></div>
        <button type="button" class="btn btn--primary" data-scan>Find samples</button></div>
      <div data-libs></div></section>
    <section class="card wb-card" data-cr><h3>2 · Cell Ranger</h3>
      <div class="wb-grid3">
        ${pathField('output', 'Output folder', store.get('sc:output'), { hint: 'One folder per sample (its run name), plus <span class="mono">seurat/</span>' })}
        <label class="wb-field"><span>Genome</span><select class="input" name="genome"><option>hg38</option><option>mm10</option><option>mm39</option></select></label>
        <label class="wb-field"><span>Custom reference <small class="muted">(optional, overrides the genome)</small></span><input class="input mono" name="reference" placeholder="/…/refdata-…" /></label>
        <label class="wb-field"><span>Cores</span><input class="input" type="number" name="threads" min="1" value="${esc(store.get('sc:threads', '20'))}" /></label>
        <label class="wb-field"><span>Memory (GB)</span><input class="input" type="number" name="memory" min="8" value="${esc(store.get('sc:memory', '200'))}" /></label>
        <label class="wb-field" data-gex-only><span>Cell Ranger version</span><select class="input" name="rna_version"><option>10</option><option>9</option><option>8</option><option>7</option></select></label>
        <label class="wb-check" data-bam><input type="checkbox" name="create_bam" checked /> Write the BAM</label>
        <label class="wb-check"><input type="checkbox" name="secondary" checked /> Cell Ranger's own clustering</label>
        <label class="wb-check"><input type="checkbox" name="aggr" /> Also aggregate all samples (cellranger aggr, for the Loupe browser)</label>
      </div></section>
    <section class="card wb-card"><h3>3 · Seurat analysis</h3>
      <label class="wb-check"><input type="checkbox" name="seurat" checked /> Analyse with Seurat / Signac after Cell Ranger</label>
      <div data-seurat class="wb-grid3">
        <label class="wb-check"><input type="checkbox" name="review" checked /> Let me review the QC thresholds before filtering</label>
        <label class="wb-field"><span>Threshold width (MADs)</span><input class="input" type="number" step="0.5" min="1" name="nmad" value="3" /></label>
        <label class="wb-check"><input type="checkbox" name="integrate" checked /> Harmony integration by sample</label>
        <label class="wb-field"><span>Cluster resolutions</span><input class="input mono" name="resolution" value="0.4,0.8" /></label>
        <label class="wb-check"><input type="checkbox" name="doublets" /> Remove doublets (scDblFinder, if installed)</label>
        <div class="wb-field"><span>Save as</span><span class="wb-inline"><label class="wb-check"><input type="checkbox" name="f_rds" checked /> RDS</label>
          <label class="wb-check" data-fmt-ad><input type="checkbox" name="f_ad" checked /> <span data-fmt-name>h5ad</span></label></span></div>
        <fieldset class="wb-fieldset wb-span3"><legend>Label transfer <small class="muted">(optional)</small></legend>
          <div class="wb-grid3">
            ${pathField('ref', 'Reference (Seurat .rds with RNA counts)', store.get('sc:ref'), { files: true })}
            <label class="wb-field"><span>Label column <button type="button" class="btn btn--quiet btn--xs" data-inspect>read columns</button></span>
              <select class="input" name="ref_label"><option value="auto">detect (celltype, predicted.id, cluster type…)</option></select></label>
            <label class="wb-field"><span>Minimum prediction score</span><input class="input" type="number" step="0.05" min="0" max="1" name="ref_min_score" value="0.5" /></label>
          </div><p class="muted small" data-ref-note>Cells below the score are labelled Unassigned.</p></fieldset>
      </div></section>
    <section class="card wb-card" data-run></section>
    <div class="wb-actions"><button type="button" class="btn btn--light" data-check>Check paths on the server</button>
      <button type="button" class="btn btn--light" data-preview>Show script</button><span class="spacer"></span><button type="button" class="btn btn--primary" data-start>Start</button></div>`;
  bindBrowse(root, { ref: /\.(rds|h5seurat)$/i });
  const run = new RunPanel($('[data-run]', root), 'analysis');
  const val = (n: string) => root.querySelector<HTMLInputElement>(`[name="${n}"]`)?.value.trim() ?? '';
  const on = (n: string) => !!root.querySelector<HTMLInputElement>(`[name="${n}"]`)?.checked;

  function renderMode() {
    $$('[data-mode]', root).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    $('[data-mode-note]', root).textContent = MODES.find((m) => m[0] === mode)![2];
    $('[data-cr]', root).hidden = false;
    // Seurat only: the output folder and the genome (mitochondrial genes, annotation) stay; Cell Ranger's options go
    $$('[data-cr] .wb-grid3 > *', root).forEach((el) => { if (!el.querySelector('[name=output], [name=genome]')) el.toggleAttribute('hidden', mode === 'seurat'); });
    $('[data-cr] h3', root).textContent = mode === 'seurat' ? '2 · Output' : '2 · Cell Ranger';
    $$('[data-gex-only]', root).forEach((el) => (el.hidden = mode !== 'gex'));
    $('[data-bam]', root).hidden = mode === 'atac' || mode === 'seurat';
    $('[data-atac-folder]', root).innerHTML = mode === 'multiome' ? pathField('fastq_atac', 'ATAC FASTQ folder (if separate)', store.get('sc:fastq_atac'), { placeholder: 'same as above' }) : '';
    $('[data-fastq-row] .wb-field span', root).textContent = mode === 'seurat' ? 'Folder with Cell Ranger outputs (searched 4 levels deep)' : mode === 'multiome' ? 'GEX FASTQ folder (or both)' : 'FASTQ folder';
    $('[data-scan]', root).textContent = mode === 'seurat' ? 'Find Cell Ranger runs' : 'Find samples';
    $('[data-fmt-name]', root).textContent = mode === 'multiome' ? 'h5mu' : 'h5ad';
    $<HTMLInputElement>('[name=seurat]', root).disabled = mode === 'seurat';
    if (mode === 'seurat') $<HTMLInputElement>('[name=seurat]', root).checked = true;
    $('[data-seurat]', root).hidden = !on('seurat');
    renderLibs();
  }

  function clearFound() { libs = []; pairs = []; runs = []; }

  function renderLibs() {
    const box = $('[data-libs]', root);
    if (mode === 'seurat') {
      box.innerHTML = runs.length ? `<div class="table-wrap"><table class="table wb-table"><thead><tr><th></th><th>Sample name</th><th>Kind</th><th>outs folder</th></tr></thead><tbody>
        ${runs.map((r, i) => `<tr data-run-i="${i}" class="${r.include === false ? 'off' : ''}"><td><input type="checkbox" data-rf="include" ${r.include === false ? '' : 'checked'} /></td>
          <td><input class="input input--sm mono" data-rf="name" value="${esc(r.name)}" /></td><td>${esc(r.kind)}${r.aggr ? ' (aggr)' : ''}</td><td class="mono small">${esc(r.outs)}</td></tr>`).join('')}</tbody></table></div>
        <p class="muted small">Analysed with the mode of the outputs (${esc([...new Set(runs.filter((r) => r.include !== false).map((r) => r.kind))].join(', ') || '–')}): choose runs of one kind. An aggr output is one sample per library.</p>`
        : '<p class="muted small">Give the folder and press Find Cell Ranger runs.</p>';
      return;
    }
    if (!libs.length) { box.innerHTML = '<p class="muted small">Give the folder and press Find samples (10x file names: <span class="mono">&lt;sample&gt;_S1_L001_R1_001.fastq.gz</span>).</p>'; return; }
    if (mode === 'multiome') {
      const opts = (kind: string, sel: string) => `<option value="">—</option>${libs.map((l) => `<option value="${l.id}" ${l.id === sel ? 'selected' : ''}>${esc(libLabel(l))}${l.kind !== kind ? ' ?' : ''}</option>`).join('')}`;
      box.innerHTML = `<div class="table-wrap"><table class="table wb-table"><thead><tr><th></th><th>Run name</th><th>GEX library</th><th>ATAC library</th></tr></thead><tbody>
        ${pairs.map((p, i) => `<tr data-pair-i="${i}" class="${p.include === false ? 'off' : ''}"><td><input type="checkbox" data-pf="include" ${p.include === false ? '' : 'checked'} /></td>
          <td><input class="input input--sm mono" data-pf="name" value="${esc(p.name)}" /></td>
          <td><select class="input input--sm" data-pf="gex">${opts('gex', p.gex)}</select></td><td><select class="input input--sm" data-pf="atac">${opts('atac', p.atac)}</select></td></tr>`).join('')}
        </tbody></table></div><div class="wb-row"><button type="button" class="btn btn--light btn--sm" data-add-pair>Add a pair</button>
        <span class="muted small">Libraries were paired by name without GEX / ATAC, index and flowcell; the sequencing runs of a library (other flowcells) go
          together, and copies in <span class="mono">merged/</span> folders are offered but not chosen. ? marks a library that looks like the other kind.</span></div>
        <details><summary class="small">Libraries found (${libs.length})</summary>${libTable(false)}</details>`;
    } else box.innerHTML = libTable(true);
  }
  const libLabel = (l: Lib) => `${l.key}${l.merged ? ' (merged copy)' : ''} · ${l.runs} run${l.runs === 1 ? '' : 's'}`;
  function libTable(editable: boolean) {
    return `<div class="table-wrap"><table class="table wb-table"><thead><tr>${editable ? '<th></th><th>Run name</th>' : ''}<th>Library</th><th>Reads</th><th>Folders</th><th class="num">Size</th></tr></thead><tbody>
      ${libs.map((l, i) => `<tr data-lib-i="${i}" class="${l.include === false ? 'off' : ''}">${editable ? `<td><input type="checkbox" data-lf="include" ${l.include === false ? '' : 'checked'} /></td>
        <td><input class="input input--sm mono" data-lf="name" value="${esc(l.name)}" /></td>` : ''}
        <td><span class="mono">${esc(l.key)}</span>${l.merged ? ' <span class="chip chip--warn">merged copy</span>' : ''}<br>
          <span class="muted small" title="${esc(l.prefix.split(',').join('\n'))}">${l.runs} sequencing run${l.runs === 1 ? '' : 's'}</span></td>
        <td class="small">${esc(l.kind.toUpperCase())} · ${esc(l.reads.join(' '))} · ${l.files} files</td><td class="mono small">${l.dirs.map(esc).join('<br>')}</td><td class="num">${fmtBytes(l.size)}</td></tr>`).join('')}
      </tbody></table></div>`;
  }

  root.addEventListener('click', async (ev) => {
    const t = ev.target as HTMLElement;
    const m = t.closest<HTMLElement>('[data-mode]');
    if (m) { mode = m.dataset.mode!; store.set('sc:mode', mode); libs = []; pairs = []; runs = []; renderMode(); }
    if (t.closest('[data-add-pair]')) { pairs.push({ gex: '', atac: '', name: `sample${pairs.length + 1}` }); renderLibs(); }
    if (t.closest('[data-inspect]')) inspectReference(true);
  });
  root.addEventListener('change', (ev) => {
    const t = ev.target as HTMLInputElement;
    const v = t.type === 'checkbox' ? t.checked : t.value.trim();
    const li = t.closest<HTMLElement>('[data-lib-i]'), pi = t.closest<HTMLElement>('[data-pair-i]'), ri = t.closest<HTMLElement>('[data-run-i]');
    if (li && t.dataset.lf) { (libs[+li.dataset.libI!] as any)[t.dataset.lf] = v; li.classList.toggle('off', libs[+li.dataset.libI!].include === false); }
    if (pi && t.dataset.pf) { (pairs[+pi.dataset.pairI!] as any)[t.dataset.pf] = v; pi.classList.toggle('off', pairs[+pi.dataset.pairI!].include === false); }
    if (ri && t.dataset.rf) { (runs[+ri.dataset.runI!] as any)[t.dataset.rf] = v; renderLibs(); }
    if (t.name === 'seurat') $('[data-seurat]', root).hidden = !t.checked;
    // another folder: what was found in the previous one no longer applies
    if (t.name === 'fastq' || t.name === 'fastq_atac') { clearFound(); renderLibs(); }
    if (['threads', 'memory', 'output', 'ref', 'fastq_atac'].includes(t.name)) store.set(`sc:${t.name}`, t.value.trim());
    if (t.name === 'ref') { refColumns = []; renderRefColumns(); inspectReference(false); }
  });

  function renderRefColumns(suggested?: string) {
    const sel = $<HTMLSelectElement>('[name=ref_label]', root);
    sel.innerHTML = `<option value="auto">detect (celltype, predicted.id, cluster type…)</option>${refColumns.map((c) =>
      `<option value="${esc(c.name)}" ${c.name === suggested ? 'selected' : ''}>${esc(c.name)} — ${c.levels} labels (${esc(c.examples.slice(0, 3).join(', '))})</option>`).join('')}`;
  }
  let inspecting = 0;
  async function inspectReference(start: boolean) {
    const path = val('ref');
    if (!path) { if (start) toast('Choose the reference file first'); return; }
    const note = $('[data-ref-note]', root);
    const ticket = ++inspecting;
    try {
      let r = await wb<any>('reference', { path, start });
      while (r.state === 'running' && ticket === inspecting) {
        note.textContent = `Reading the reference's columns… (large objects take a few minutes)`;
        await new Promise((res) => setTimeout(res, 4000));
        r = await wb<any>('reference', { path });
      }
      if (ticket !== inspecting) return;
      if (r.state === 'done') {
        refColumns = r.result.columns ?? [];
        renderRefColumns(r.result.suggested);
        note.textContent = `${(r.result.cells ?? 0).toLocaleString()} cells; ${refColumns.length} label columns. Cells below the score are labelled Unassigned.`;
      } else if (r.state === 'failed') note.innerHTML = `<span class="err">${esc(r.error)}</span>`;
      else note.textContent = 'Press “read columns” to list the reference’s label columns (or keep “detect”).';
    } catch (err) { note.innerHTML = `<span class="err">${esc((err as Error).message)}</span>`; }
  }

  $('[data-scan]', root).onclick = async () => {
    const folder = val('fastq');
    if (!folder) return toast('Give the folder');
    store.set('sc:fastq', folder);
    const b = $<HTMLButtonElement>('[data-scan]', root);
    b.disabled = true;
    clearFound();
    $('[data-libs]', root).innerHTML = '<p class="muted small">Searching…</p>';
    try {
      const choice = await run.choice();
      if (mode === 'seurat') {
        runs = (await wb<{ runs: Run[] }>('scan', { ...choice, folder, kind: 'outs' })).runs;
        if (!val('output')) $<HTMLInputElement>('[name=output]', root).value = folder;
        toast(`${runs.length} Cell Ranger outputs`);
      } else {
        // multiome: an ATAC folder of its own is scanned with the first; libraries are grouped and paired on the server
        const atacFolder = mode === 'multiome' ? val('fastq_atac') : '';
        const r = await wb<{ samples: Lib[]; pairs: Pair[] }>('scan', { ...choice, folder, folders: atacFolder ? [atacFolder] : [], kind: '10x' });
        libs = r.samples;
        pairs = r.pairs;
        if (!val('output')) $<HTMLInputElement>('[name=output]', root).value = `${folder.replace(/\/$/, '')}_cellranger`;
        toast(`${libs.length} libraries`);
      }
      renderLibs();
    } catch (err) { toast(esc((err as Error).message)); }
    finally { b.disabled = false; }
  };

  function params() {
    const lib = (id: string) => { const l = libs.find((x) => x.id === id); return l ? { prefix: l.prefix, dirs: l.dirs, parts: l.parts } : null; };
    const included = runs.filter((r) => r.include !== false);
    const kinds = [...new Set(included.map((r) => r.kind))];
    const m = mode === 'seurat' ? kinds[0] ?? 'gex' : mode;
    return {
      mode: m, output: val('output'), genome: val('genome'), reference: val('reference'), threads: +val('threads') || 16, memory: +val('memory') || 128,
      rna_version: val('rna_version'), create_bam: on('create_bam'), secondary: on('secondary'), aggr: on('aggr'),
      skip_count: mode === 'seurat', existing: mode === 'seurat' ? included.map((r) => ({ name: r.name, outs: r.outs })) : undefined,
      samples: m === 'multiome' ? undefined : libs.map((l) => ({ name: l.name, prefix: l.prefix, dirs: l.dirs, include: l.include !== false })),
      pairs: m === 'multiome' ? pairs.map((p) => ({ name: p.name, gex: lib(p.gex), atac: lib(p.atac), include: p.include !== false })) : undefined,
      seurat: on('seurat') ? {
        run: true, review: on('review'), nmad: +val('nmad') || 3, integrate: on('integrate'), resolution: val('resolution'), doublets: on('doublets'),
        formats: [on('f_rds') && 'rds', on('f_ad') && (m === 'multiome' ? 'h5mu' : 'h5ad')].filter(Boolean),
        ref: val('ref'), ref_label: val('ref_label') || 'auto', ref_min_score: +val('ref_min_score') || 0.5,
      } : undefined,
    };
  }
  const guard = () => {
    if (mode === 'seurat') {
      const kinds = new Set(runs.filter((r) => r.include !== false).map((r) => r.kind));
      if (!kinds.size) throw new Error('Find and choose the Cell Ranger outputs first');
      if (kinds.size > 1) throw new Error('Choose outputs of one kind (GEX, ATAC or multiome)');
    } else if (!libs.length) throw new Error('Find the samples first');
    if (!val('output').startsWith('/')) throw new Error('Give the output folder');
  };
  $('[data-check]', root).onclick = async () => {
    try { guard(); run.showChecks(await wb('check', { ...(await run.choice()), kind: 'singlecell', params: params() })); }
    catch (err) { toast(esc((err as Error).message)); }
  };
  $('[data-preview]', root).onclick = async () => {
    try { guard(); showScript((await wb<{ script: string }>('singlecell/preview', { params: params() })).script); }
    catch (err) { toast(esc((err as Error).message)); }
  };
  $('[data-start]', root).onclick = async () => {
    const b = $<HTMLButtonElement>('[data-start]', root);
    try {
      guard();
      b.disabled = true;
      const choice = await run.choice();
      const check = await wb<any>('check', { ...choice, kind: 'singlecell', params: params() });
      run.showChecks(check);
      if (!check.ok && !confirm('Some paths or programs were not found on that server (see the list). Start anyway?')) return;
      const job = await wb<{ id: string }>('singlecell/run', { ...choice, params: params() });
      location.href = `/workbench/jobs/${job.id}/`;
    } catch (err) { toast(esc((err as Error).message)); }
    finally { b.disabled = false; }
  };
  renderMode();
}
