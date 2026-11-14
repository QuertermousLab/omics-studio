// Bulk sequencing: RNA-seq, ChIP-seq, CUT&RUN, CUT&Tag, ATAC-seq with the lab's Pipelines-Wrappers.

import { $, $$, RunPanel, bindBrowse, esc, fmtBytes, pathField, showScript, store, toast, wb } from './common';

interface Sample {
  id: string; prefix: string; name: string; dir: string; r1: string[]; r2: string[]; paired: boolean; size: number; lanes: number;
  problem: string | null; group: string; is_control: boolean; control: string; include?: boolean;
}

const ASSAYS: [string, string, string][] = [
  ['rnaseq', 'RNA-seq', 'RNAseq.sh: cutadapt, STAR, featureCounts, TPM and DESeq2 between groups'],
  ['chipseq', 'ChIP-seq', 'ChIPseq.sh: BWA (or chromap), filtered BAM / BED, bigWig; then MACS2 with input / IgG'],
  ['cutrun', 'CUT&RUN', 'ChIPseq.sh -R: Bowtie2, TruSeq adapters; then MACS2'],
  ['cuttag', 'CUT&Tag', 'ChIPseq.sh -T: Bowtie2, Nextera adapters; then MACS2'],
  ['atacseq', 'ATAC-seq', 'ATACseq.sh: Bowtie2 (or chromap), Tn5 shift, MACS2 peaks, blacklist filter'],
];
const MACS2_ATAC = '--keep-dup all --nomodel --shift -75 --extsize 150 -q 0.05';

export function bulkPage(root: HTMLElement) {
  let assay = store.get('bulk:assay', 'rnaseq');
  let samples: Sample[] = [];
  let comparisons: { a: string; b: string; on: boolean }[] = [];
  root.innerHTML = `<header class="wb-head"><h1>Bulk sequencing</h1>
      <p class="muted">Choose the assay and the FASTQ folder: the samples are found and named for you to edit, then the lab's pipeline runs on the server you choose, in the background.</p></header>
    <section class="card wb-card"><h3>1 · Assay</h3><div class="wb-seg" data-assays>${ASSAYS.map(([id, label]) => `<button type="button" data-assay="${id}">${label}</button>`).join('')}</div>
      <p class="muted small" data-assay-note></p></section>
    <section class="card wb-card"><h3>2 · FASTQ files</h3>
      <div class="wb-row">${pathField('fastq', 'FASTQ folder (searched 4 levels deep)', store.get('bulk:fastq'))}<button type="button" class="btn btn--primary" data-scan>Find samples</button></div>
      <div data-samples></div><div data-comparisons></div></section>
    <section class="card wb-card"><h3>3 · Output and options</h3>
      <div class="wb-grid3">
        ${pathField('output', 'Output folder', store.get('bulk:output'), { hint: 'Created if missing; results, logs and <span class="mono">fastq/</span> links go here' })}
        <label class="wb-field"><span>Genome</span><select class="input" name="genome"><option>hg38</option><option>hg19</option><option>mm10</option></select></label>
        <label class="wb-field"><span>Threads</span><input class="input" name="threads" type="number" min="1" max="256" value="${esc(store.get('bulk:threads', '16'))}" /></label>
      </div>
      <div class="wb-opts" data-opts></div></section>
    <section class="card wb-card" data-run></section>
    <div class="wb-actions"><button type="button" class="btn btn--light" data-check>Check paths on the server</button>
      <button type="button" class="btn btn--light" data-preview>Show script</button><span class="spacer"></span>
      <button type="button" class="btn btn--primary" data-start>Start</button></div>`;
  bindBrowse(root);
  const run = new RunPanel($('[data-run]', root), 'pipeline');
  const val = (n: string) => $<HTMLInputElement>(`[name="${n}"]`, root).value.trim();
  const checked = (n: string) => !!root.querySelector<HTMLInputElement>(`[name="${n}"]`)?.checked;

  function renderAssay() {
    $$('[data-assay]', root).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.assay === assay)));
    $('[data-assay-note]', root).textContent = ASSAYS.find((a) => a[0] === assay)![2];
    const genome = $<HTMLSelectElement>('[name=genome]', root);
    genome.querySelector<HTMLOptionElement>('option[value=hg19], option:nth-child(2)')!.disabled = assay === 'rnaseq';
    if (assay === 'rnaseq' && genome.value === 'hg19') genome.value = 'hg38';
    const chip = assay === 'chipseq' || assay === 'cutrun' || assay === 'cuttag';
    $('[data-opts]', root).innerHTML = `
      <div class="wb-grid3">
        ${assay === 'rnaseq' ? `<label class="wb-check"><input type="checkbox" name="nextera" /> Nextera adapters <small class="muted">(TruSeq by default)</small></label>
          <label class="wb-field"><span>Custom STAR index <small class="muted">(optional, with its GTF)</small></span><input class="input mono" name="star_index" placeholder="default: the genome's" /></label>
          <label class="wb-field"><span>Custom GTF</span><input class="input mono" name="gtf" placeholder="default: GENCODE of the genome" /></label>` : ''}
        ${assay !== 'rnaseq' ? `<label class="wb-field"><span>Aligner</span><select class="input" name="aligner">
            ${assay === 'chipseq' ? '<option value="">BWA mem</option><option value="bwa-aln">BWA aln (short reads)</option>' : assay === 'atacseq' ? '<option value="">Bowtie2</option>' : '<option value="">Bowtie2</option>'}
            <option value="chromap">chromap (fast)</option></select></label>
          <label class="wb-check"><input type="checkbox" name="single" /> Single-end reads</label>` : ''}
        ${chip ? `<label class="wb-field"><span>Minimum MAPQ</span><input class="input" name="mapq" type="number" min="0" value="30" /></label>
          <label class="wb-check"><input type="checkbox" name="nextera" /> Force Nextera adapters</label>` : ''}
        <label class="wb-check"><input type="checkbox" name="continue" ${assay === 'rnaseq' ? 'disabled' : ''} /> Continue with the other samples when one fails</label>
      </div>
      ${chip ? `<fieldset class="wb-fieldset"><legend><label class="wb-check"><input type="checkbox" name="peaks" checked /> Call peaks with MACS2</label></legend>
        <div class="wb-grid3">
          <label class="wb-field"><span>Peaks</span><select class="input" name="peaktype"><option value="narrow">narrow (TFs, H3K4me3, H3K27ac)</option><option value="broad">broad (H3K27me3, H3K36me3, H3K9me3)</option></select></label>
          <label class="wb-field"><span>q-value cutoff</span><input class="input" name="qvalue" value="0.05" /></label>
          <label class="wb-field"><span>Broad cutoff</span><input class="input" name="broad_cutoff" value="0.1" /></label>
          <label class="wb-field wb-span3"><span>Other MACS2 options</span><input class="input mono" name="macs_extra" placeholder="e.g. --keep-dup all --SPMR" /></label>
        </div><p class="muted small">Each sample's control (input or IgG) is set in the sample table; without one MACS2 runs without control. Controls are aligned but not peak-called.</p></fieldset>` : ''}
      ${assay === 'atacseq' ? `<label class="wb-field"><span>MACS2 callpeak options <button type="button" class="btn btn--quiet btn--xs" data-macs-reset>default</button></span>
        <input class="input mono" name="macs2" value="${esc(store.get('bulk:macs2', MACS2_ATAC))}" /><small class="muted">After <span class="mono">-t &lt;Tn5-shifted reads&gt; -g &lt;genome&gt; -n &lt;sample&gt; -f BED</span>; peaks are then filtered with the ENCODE blacklist.</small></label>` : ''}`;
    renderSamples();
  }

  function groups() { return [...new Set(samples.filter((s) => s.include !== false).map((s) => s.group))]; }

  function renderSamples() {
    const box = $('[data-samples]', root);
    if (!samples.length) { box.innerHTML = '<p class="muted small">No samples yet: give the folder and press Find samples.</p>'; $('[data-comparisons]', root).innerHTML = ''; return; }
    const chip = assay === 'chipseq' || assay === 'cutrun' || assay === 'cuttag';
    const names = samples.filter((s) => s.include !== false).map((s) => s.name);
    box.innerHTML = `<div class="table-wrap"><table class="table wb-table"><thead><tr><th><input type="checkbox" data-all checked title="All" /></th><th>Sample name</th>
      ${assay === 'rnaseq' ? '<th>Group</th>' : ''}${chip ? '<th>Control?</th><th>Its control</th>' : ''}<th>Files</th><th class="num">Size</th></tr></thead><tbody>
      ${samples.map((s) => `<tr data-id="${s.id}" class="${s.include === false ? 'off' : ''}">
        <td><input type="checkbox" data-f="include" ${s.include === false ? '' : 'checked'} /></td>
        <td><input class="input input--sm mono" data-f="name" value="${esc(s.name)}" /></td>
        ${assay === 'rnaseq' ? `<td><input class="input input--sm" data-f="group" value="${esc(s.group)}" list="wb-groups" /></td>` : ''}
        ${chip ? `<td><input type="checkbox" data-f="is_control" ${s.is_control ? 'checked' : ''} title="Input or IgG" /></td>
          <td>${s.is_control ? '<span class="muted small">—</span>' : `<select class="input input--sm" data-f="control"><option value="">none</option>${samples.filter((c) => c.is_control && c.include !== false)
            .map((c) => `<option ${c.name === s.control ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select>`}</td>` : ''}
        <td class="small" title="${esc([...s.r1, ...s.r2].join('\n'))}"><span class="mono">${esc(s.prefix)}</span><br><span class="muted">${s.paired ? 'paired' : 'single'} · ${s.lanes} lane${s.lanes > 1 ? 's (joined)' : ''} · ${esc(s.dir)}</span>
          ${s.problem ? `<br><span class="err">${esc(s.problem)}</span>` : ''}</td>
        <td class="num">${fmtBytes(s.size)}</td></tr>`).join('')}</tbody></table></div>
      <datalist id="wb-groups">${groups().map((g) => `<option>${esc(g)}</option>`).join('')}</datalist>
      <p class="muted small">${names.length} of ${samples.length} samples. Names become the output prefixes (letters, digits, _ . -).${assay === 'rnaseq' ? ' Groups were guessed from the names without their replicate number.' : ''}</p>`;
    renderComparisons();
  }

  function renderComparisons() {
    const box = $('[data-comparisons]', root);
    if (assay !== 'rnaseq') { box.innerHTML = ''; return; }
    const g = groups();
    // keep the user's choices; add new pairs (on), drop pairs of removed groups
    const key = (a: string, b: string) => [a, b].sort().join('\u0000');
    const known = new Map(comparisons.map((c) => [key(c.a, c.b), c]));
    const next: typeof comparisons = [];
    for (let i = 0; i < g.length; i++) for (let j = i + 1; j < g.length; j++) next.push(known.get(key(g[i], g[j])) ?? { a: g[i], b: g[j], on: true });
    comparisons = next;
    box.innerHTML = `<h4>DESeq2 comparisons</h4>${g.length < 2 ? '<p class="err small">DESeq2 needs at least two groups.</p>' : `
      <p class="muted small">Every pair of groups is compared unless you untick it; ⇄ swaps which group is the numerator (log2 fold change of the first over the second).</p>
      <div class="wb-pairs">${comparisons.map((c, i) => `<label class="wb-pair ${c.on ? '' : 'off'}"><input type="checkbox" data-pair="${i}" ${c.on ? 'checked' : ''} />
        <b>${esc(c.a)}</b> vs <b>${esc(c.b)}</b><button type="button" class="btn btn--quiet btn--xs" data-swap="${i}" title="Swap">⇄</button></label>`).join('')}</div>`}`;
  }

  root.addEventListener('click', (ev) => {
    const t = ev.target as HTMLElement;
    const a = t.closest<HTMLElement>('[data-assay]');
    if (a) { assay = a.dataset.assay!; store.set('bulk:assay', assay); renderAssay(); }
    const sw = t.closest<HTMLElement>('[data-swap]');
    if (sw) { const c = comparisons[+sw.dataset.swap!]; [c.a, c.b] = [c.b, c.a]; renderComparisons(); }
    if (t.closest('[data-macs-reset]')) $<HTMLInputElement>('[name=macs2]', root).value = MACS2_ATAC;
  });
  root.addEventListener('change', (ev) => {
    const t = ev.target as HTMLInputElement;
    if (t.dataset.pair) { comparisons[+t.dataset.pair].on = t.checked; renderComparisons(); return; }
    if (t.dataset.all !== undefined) { samples.forEach((s) => (s.include = t.checked)); renderSamples(); return; }
    const row = t.closest<HTMLElement>('tr[data-id]');
    if (row && t.dataset.f) {
      const s = samples.find((x) => x.id === row.dataset.id)!;
      const f = t.dataset.f as keyof Sample;
      const old = s.name;
      (s as any)[f] = t.type === 'checkbox' ? t.checked : t.value.trim();
      if (f === 'name') samples.forEach((x) => { if (x.control === old) x.control = s.name; });
      if (f !== 'name' && f !== 'control') renderSamples(); else if (f === 'name') renderSamples();
      else renderComparisons();
    }
    if (t.name === 'macs2') store.set('bulk:macs2', t.value);
    if (t.name === 'threads') store.set('bulk:threads', t.value);
    if (t.name === 'output') store.set('bulk:output', t.value.trim());
    // another folder: the samples found in the previous one no longer apply
    if (t.name === 'fastq') { samples = []; comparisons = []; renderSamples(); }
  });

  $('[data-scan]', root).onclick = async () => {
    const folder = val('fastq');
    if (!folder) return toast('Give the FASTQ folder');
    store.set('bulk:fastq', folder);
    const b = $<HTMLButtonElement>('[data-scan]', root);
    b.disabled = true; b.textContent = 'Searching…';
    samples = []; comparisons = [];
    renderSamples();
    try {
      const r = await wb<{ files: number; samples: Sample[]; paired: boolean }>('scan', { ...(await run.choice()), folder, kind: 'bulk' });
      samples = r.samples;
      comparisons = [];
      const single = root.querySelector<HTMLInputElement>('[name=single]');
      if (single) single.checked = !r.paired;
      if (!val('output')) $<HTMLInputElement>('[name=output]', root).value = `${folder.replace(/\/$/, '')}_${assay}`;
      renderSamples();
      toast(`${r.files} FASTQ files, ${samples.length} samples`);
    } catch (err) { toast(esc((err as Error).message)); }
    finally { b.disabled = false; b.textContent = 'Find samples'; }
  };

  function params() {
    const chip = assay === 'chipseq' || assay === 'cutrun' || assay === 'cuttag';
    const included = samples.filter((s) => s.include !== false);
    return {
      assay, output: val('output'), genome: val('genome'), threads: +val('threads') || 8,
      paired: !checked('single') && included.every((s) => s.r2.length > 0),
      nextera: checked('nextera'), aligner: root.querySelector<HTMLSelectElement>('[name=aligner]')?.value ?? '',
      mapq: chip ? val('mapq') : undefined, continue_on_error: checked('continue'),
      star_index: assay === 'rnaseq' ? val('star_index') : '', gtf: assay === 'rnaseq' ? val('gtf') : '',
      macs2: assay === 'atacseq' ? val('macs2') : undefined,
      comparisons: assay === 'rnaseq' ? comparisons.filter((c) => c.on).map((c) => [c.a, c.b]) : [],
      peaks: chip ? { call: checked('peaks'), broad: val('peaktype') === 'broad', qvalue: val('qvalue'), broad_cutoff: val('broad_cutoff'), extra: val('macs_extra') } : undefined,
      samples: samples.map((s) => ({ name: s.name, r1: s.r1, r2: s.r2, group: s.group, control: s.is_control ? '' : s.control, is_control: s.is_control, include: s.include !== false })),
    };
  }
  const guard = () => {
    if (!samples.some((s) => s.include !== false)) throw new Error('Find and choose the samples first');
    if (!val('output').startsWith('/')) throw new Error('Give the output folder');
  };
  $('[data-check]', root).onclick = async () => {
    try { guard(); run.showChecks(await wb('check', { ...(await run.choice()), kind: 'bulk', params: params() })); }
    catch (err) { toast(esc((err as Error).message)); }
  };
  $('[data-preview]', root).onclick = async () => {
    try { guard(); showScript((await wb<{ script: string }>('bulk/preview', { params: params() })).script); }
    catch (err) { toast(esc((err as Error).message)); }
  };
  $('[data-start]', root).onclick = async () => {
    const b = $<HTMLButtonElement>('[data-start]', root);
    try {
      guard();
      b.disabled = true;
      const choice = await run.choice();
      const check = await wb<any>('check', { ...choice, kind: 'bulk', params: params() });
      run.showChecks(check);
      if (!check.ok && !confirm('Some paths or programs were not found on that server (see the list). Start anyway?')) return;
      const job = await wb<{ id: string }>('bulk/run', { ...choice, params: params() });
      location.href = `/workbench/jobs/${job.id}/`;
    } catch (err) { toast(esc((err as Error).message)); }
    finally { b.disabled = false; }
  };
  renderAssay();
}
