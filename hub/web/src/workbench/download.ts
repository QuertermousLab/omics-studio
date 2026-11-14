// Download: links, provider delivery e-mails, s3:// addresses, GEO / SRA accessions -> a folder on the server.

import { $, RunPanel, bindBrowse, esc, fmtBytes, pathField, showScript, store, toast, wb } from './common';

interface Transport { scheme: string; host?: string; port?: number; path?: string; user?: string; has_password?: boolean }
interface Source { id: string; kind: string; label: string; transports?: Transport[]; bucket?: string; key?: string; profile?: string; region?: string; accession?: string }
interface Entry { path: string; bytes: number | null; dir: boolean }
interface GeoFile { name: string; url: string; bytes?: number | null }
interface GeoRun { run: string; layout?: string; bytes?: number; reads?: number; fastq?: { url: string; bytes?: number; md5?: string }[] }
interface GeoSample { gsm: string; title: string; runs: GeoRun[]; supplementary: GeoFile[] }
interface Geo { accession: string; title: string; summary?: string; samples: GeoSample[]; series_supplementary: GeoFile[]; warnings?: string[] }

const EXAMPLE = `Paste one or more of:
  ftp://… or https://… (a file or a folder)
  sftp://user@host:port/path
  a whole delivery e-mail (Novogene, MedGenome, …): host, port, account and password are read from it
  s3://bucket/path/to/folder
  a GEO or SRA accession: GSE123456, GSM…, SRP…, PRJNA…, SRR…`;

export function downloadPage(root: HTMLElement) {
  let sources: Source[] = [];
  let current: Source | null = null;
  let entries: Entry[] = [];
  let selected = new Set<string>();
  let geo: Geo | null = null;
  root.innerHTML = `<header class="wb-head"><h1>Download data</h1>
      <p class="muted">Sequencing deliveries and public data to a folder under <span class="mono">/nfs</span>, downloaded on THOR in your account. SFTP, lftp, scp
      and HTTPS are tried in turn (ports are sometimes blocked); downloads resume after interruptions and MD5 sums are checked when the delivery has them.</p></header>
    <section class="card wb-card"><h3>1 · What to download</h3>
      <textarea class="input mono wb-paste" name="paste" rows="7" spellcheck="false" placeholder="${esc(EXAMPLE)}"></textarea>
      <div class="wb-row"><button type="button" class="btn btn--primary" data-read>Read</button>
        <span class="muted small">Passwords in a pasted e-mail stay on the server (in memory) and are given to the download only.</span></div>
      <div data-warnings></div><div data-sources class="wb-sources"></div></section>
    <section class="card wb-card" data-pick hidden><h3>2 · Choose files</h3><div data-pick-body></div></section>
    <section class="card wb-card"><h3>3 · Where to</h3>
      <div class="wb-grid3">${pathField('dest', 'Destination folder', store.get('dl:dest'))}
        <label class="wb-field"><span>Keep the source's folders</span><select class="input" name="keep"><option value="">the delivered folder only (default)</option>
          <option value="2">its last 2 levels</option><option value="3">its last 3 levels</option><option value="all">the whole source path</option>
          <option value="0">none (its contents directly in the destination)</option></select></label>
        <label class="wb-field"><span>Subfolder <small class="muted">(optional)</small></span><input class="input mono" name="subfolder" placeholder="e.g. 2026-11_novogene" /></label>
        <label class="wb-field" data-geo-only hidden><span>GEO FASTQ names</span><select class="input" name="rename"><option value="run">SRR run accessions</option><option value="gsm_title">GSM and sample title</option></select></label>
        <label class="wb-check"><input type="checkbox" name="md5" checked /> Check MD5 sums</label>
        <label class="wb-field"><span>Parallel transfers</span><input class="input" type="number" min="1" max="32" name="parallel" value="4" /></label>
      </div>
      <div class="wb-row"><button type="button" class="btn btn--light" data-plan>Show final paths</button></div>
      <div data-plan-out></div></section>
    <section class="card wb-card" data-run></section>
    <div class="wb-actions"><button type="button" class="btn btn--light" data-check>Check the destination</button><button type="button" class="btn btn--light" data-preview>Show script</button>
      <span class="spacer"></span><button type="button" class="btn btn--primary" data-start>Start download</button></div>`;
  bindBrowse(root);
  const run = new RunPanel($('[data-run]', root), 'download', true);
  const val = (n: string) => root.querySelector<HTMLInputElement>(`[name="${n}"]`)?.value.trim() ?? '';

  me();
  async function me() {
    const m = await wb<{ download: boolean }>('me');
    if (!m.download) $('[data-sources]', root).innerHTML = '<p class="err">The download module is not installed on the server yet.</p>';
  }

  function transportText(t: Transport) {
    return `${t.scheme}://${t.user ? `${esc(t.user)}@` : ''}${esc(t.host ?? '')}${t.port ? `:${t.port}` : ''}${esc(t.path ?? '')}${t.has_password ? ' 🔑' : ''}`;
  }
  function renderSources() {
    $('[data-sources]', root).innerHTML = sources.map((s) => `<button type="button" class="wb-source ${current?.id === s.id ? 'sel' : ''}" data-source="${esc(s.id)}">
      <span class="chip">${esc(s.kind)}</span><b>${esc(s.label)}</b>
      <span class="muted small mono">${s.kind === 's3' ? `s3://${esc(s.bucket)}/${esc(s.key)} · profile ${esc(s.profile)} (${esc(s.region)})`
        : s.kind === 'geo' || s.kind === 'sra' ? esc(s.accession) : (s.transports ?? []).map(transportText).join(' · or ')}</span></button>`).join('');
  }

  $('[data-read]', root).onclick = async () => {
    const text = val('paste');
    if (!text) return toast('Paste something first');
    try {
      const r = await wb<{ sources: Source[]; warnings: string[] }>('download/parse', { text });
      sources = r.sources;
      $('[data-warnings]', root).innerHTML = r.warnings.map((w) => `<p class="notice small">${esc(w)}</p>`).join('');
      if (!sources.length) toast('Nothing to download was found in the text');
      current = null;
      renderSources();
      if (sources.length === 1) choose(sources[0].id);
    } catch (err) { toast(esc((err as Error).message)); }
  };
  $('[data-sources]', root).addEventListener('click', (ev) => {
    const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-source]');
    if (b) choose(b.dataset.source!);
  });

  async function choose(id: string) {
    current = sources.find((s) => s.id === id) ?? null;
    entries = []; selected = new Set(); geo = null;
    renderSources();
    const pick = $('[data-pick]', root), body = $('[data-pick-body]', root);
    pick.hidden = !current;
    $('[data-geo-only]', root).hidden = !(current && (current.kind === 'geo' || current.kind === 'sra'));
    if (!current) return;
    if (current.kind === 'geo' || current.kind === 'sra') {
      body.innerHTML = `<p class="muted">Looking up ${esc(current.accession)} at GEO / ENA…</p>`;
      try { geo = await wb<Geo>('download/geo', { accession: current.accession, source: current.id }); renderGeo(); }
      catch (err) { body.innerHTML = `<p class="err">${esc((err as Error).message)}</p>`; }
      return;
    }
    body.innerHTML = `<p class="muted small">Everything is downloaded unless you list the files and choose some.</p>
      <div class="wb-row"><button type="button" class="btn btn--light" data-list>List files</button><span class="muted small" data-list-note></span></div><div data-tree></div>`;
  }

  function renderGeo() {
    const body = $('[data-pick-body]', root);
    if (!geo) return;
    const fq = (r: GeoRun) => (r.fastq ?? []).reduce((a, f) => a + (f.bytes ?? 0), 0);
    body.innerHTML = `<h4>${esc(geo.accession)}: ${esc(geo.title)}</h4>${geo.summary ? `<p class="muted small wb-clamp">${esc(geo.summary)}</p>` : ''}
      ${(geo.warnings ?? []).map((w) => `<p class="notice small">${esc(w)}</p>`).join('')}
      <div class="wb-row"><button type="button" class="btn btn--light btn--sm" data-geo-all="fastq">All raw FASTQ</button>
        <button type="button" class="btn btn--light btn--sm" data-geo-all="supp">All processed files</button><button type="button" class="btn btn--quiet btn--sm" data-geo-all="none">None</button>
        <span class="muted small" data-geo-count></span></div>
      ${geo.series_supplementary.length ? `<h4>Series files (processed)</h4><ul class="wb-filelist">${geo.series_supplementary.map((f) => fileItem(`supp:${f.url}`, f.name, f.bytes)).join('')}</ul>` : ''}
      <div class="table-wrap"><table class="table wb-table"><thead><tr><th>Sample</th><th>Raw FASTQ (runs)</th><th>Processed files</th></tr></thead><tbody>
      ${geo.samples.map((s) => `<tr><td><b>${esc(s.gsm)}</b><br><span class="small">${esc(s.title)}</span></td>
        <td>${s.runs.map((r) => fileItem(`fastq:${r.run}`, `${r.run}${r.layout ? ` · ${r.layout.toLowerCase()}` : ''}`, fq(r))).join('') || '<span class="muted small">none</span>'}</td>
        <td>${s.supplementary.map((f) => fileItem(`supp:${f.url}`, f.name, f.bytes)).join('') || '<span class="muted small">none</span>'}</td></tr>`).join('')}
      </tbody></table></div>`;
    countGeo();
  }
  const fileItem = (key: string, name: string, bytes?: number | null) =>
    `<label class="wb-check wb-fileitem"><input type="checkbox" data-sel="${esc(key)}" ${selected.has(key) ? 'checked' : ''} /> <span class="mono small">${esc(name)}</span> <span class="muted small">${bytes ? fmtBytes(bytes) : ''}</span></label>`;
  function countGeo() {
    const n = $('[data-geo-count]', root);
    if (n) n.textContent = `${selected.size} chosen`;
  }

  function renderTree() {
    const box = $('[data-tree]', root);
    const total = entries.filter((e) => !e.dir).reduce((a, e) => a + (e.bytes ?? 0), 0);
    box.innerHTML = `<div class="wb-row"><label class="wb-check"><input type="checkbox" data-tree-all ${selected.size === 0 ? 'checked' : ''} /> Everything (${entries.filter((e) => !e.dir).length} files, ${fmtBytes(total)})</label></div>
      <ul class="wb-filelist wb-tree">${entries.map((e) => `<li style="padding-left:${(e.path.split('/').length - 1) * 16}px">${e.dir
        ? `<span class="small">📁 ${esc(e.path.split('/').pop()!)}/</span>`
        : fileItem(`path:${e.path}`, e.path.split('/').pop()!, e.bytes)}</li>`).join('')}</ul>`;
  }

  root.addEventListener('click', async (ev) => {
    const t = ev.target as HTMLElement;
    if (t.closest('[data-list]') && current) {
      const note = $('[data-list-note]', root);
      note.textContent = 'Listing…';
      try {
        const r = await wb<{ entries: Entry[]; transport_used: string; errors: string[] }>('download/list', { source: current.id });
        entries = r.entries;
        note.textContent = `${entries.length} entries via ${r.transport_used}${r.errors?.length ? ` (${r.errors.join('; ')})` : ''}`;
        renderTree();
      } catch (err) { note.textContent = ''; toast(esc((err as Error).message)); }
    }
    const all = t.closest<HTMLElement>('[data-geo-all]');
    if (all && geo) {
      selected = new Set();
      if (all.dataset.geoAll === 'fastq') geo.samples.forEach((s) => s.runs.forEach((r) => selected.add(`fastq:${r.run}`)));
      if (all.dataset.geoAll === 'supp') { geo.series_supplementary.forEach((f) => selected.add(`supp:${f.url}`)); geo.samples.forEach((s) => s.supplementary.forEach((f) => selected.add(`supp:${f.url}`))); }
      renderGeo();
    }
  });
  root.addEventListener('change', (ev) => {
    const t = ev.target as HTMLInputElement;
    if (t.dataset.sel) { if (t.checked) selected.add(t.dataset.sel); else selected.delete(t.dataset.sel); countGeo(); const a = root.querySelector<HTMLInputElement>('[data-tree-all]'); if (a) a.checked = selected.size === 0; }
    if (t.dataset.treeAll !== undefined && t.checked) { selected = new Set(); renderTree(); }
    if (t.name === 'dest') store.set('dl:dest', t.value.trim());
  });

  function selection() {
    if (!current) return null;
    if (current.kind === 'geo' || current.kind === 'sra') {
      if (!geo) return [];
      const out: unknown[] = [];
      for (const key of selected) {
        const [type, ...rest] = key.split(':');
        const v = rest.join(':');
        if (type === 'fastq') {
          const s = geo.samples.find((x) => x.runs.some((r) => r.run === v));
          out.push({ type: 'fastq', run: v, gsm: s?.gsm, title: s?.title });
        } else out.push({ type: 'supplementary', url: v });
      }
      return out;
    }
    if (!selected.size) return null;
    return [...selected].map((k) => k.slice(5));
  }
  function request() {
    if (!current) throw new Error('Read the source and choose it first');
    const sel = selection();
    if ((current.kind === 'geo' || current.kind === 'sra') && !(sel as unknown[]).length) throw new Error('Choose the files to download');
    if (!val('dest').startsWith('/')) throw new Error('Give the destination folder');
    const keep = val('keep');
    return {
      source: current.id, selection: sel, dest: val('dest'),
      options: { keep_levels: keep === '' || keep === 'all' ? undefined : +keep, strip_levels: keep === 'all' ? 0 : undefined,
        subfolder: val('subfolder') || undefined, rename: val('rename') || 'run',
        verify_md5: !!root.querySelector<HTMLInputElement>('[name=md5]')?.checked, parallel: +val('parallel') || 4 },
    };
  }
  $('[data-plan]', root).onclick = async () => {
    try {
      const p = await wb<{ root: string; files: { src: string; dest: string; bytes?: number }[]; notes: string[] }>('download/plan', request());
      const total = p.files.reduce((a, f) => a + (f.bytes ?? 0), 0);
      $('[data-plan-out]', root).innerHTML = `<p>Files go to <b class="mono">${esc(p.root)}</b> — ${p.files.length} item${p.files.length === 1 ? '' : 's'}${total ? `, ${fmtBytes(total)}` : ''}:</p>
        <ul class="wb-filelist wb-planlist">${p.files.slice(0, 200).map((f) => `<li class="mono small"><span class="muted">${esc(f.src)}</span> → ${esc(f.dest)}</li>`).join('')}
        ${p.files.length > 200 ? `<li class="muted small">… and ${p.files.length - 200} more</li>` : ''}</ul>${(p.notes ?? []).map((n) => `<p class="muted small">${esc(n)}</p>`).join('')}`;
    } catch (err) { toast(esc((err as Error).message)); }
  };
  $('[data-preview]', root).onclick = async () => {
    try { showScript((await wb<{ script: string }>('download/plan', request())).script, 'Download script'); }
    catch (err) { toast(esc((err as Error).message)); }
  };
  $('[data-check]', root).onclick = async () => {
    try { run.showChecks(await wb('check', { ...(await run.choice()), kind: 'download', params: { output: val('dest') } })); }
    catch (err) { toast(esc((err as Error).message)); }
  };
  $('[data-start]', root).onclick = async () => {
    const b = $<HTMLButtonElement>('[data-start]', root);
    try {
      const req = request();
      b.disabled = true;
      const job = await wb<{ id: string }>('download/run', { ...(await run.choice()), ...req });
      location.href = `/workbench/jobs/${job.id}/`;
    } catch (err) { toast(esc((err as Error).message)); }
    finally { b.disabled = false; }
  };
}
