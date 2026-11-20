// Jobs: the list, one job's page (state, live log, stop, files) and the QC threshold review of a Seurat QC job.

import { $, $$, esc, fmtDuration, fmtTime, loadServers, me, seal, serverCard, stateChip, toast, wb } from './common';

export interface Job {
  id: string; kind: string; title: string; owner: string; created: number; started: number | null; finished: number | null; server: string;
  server_name: string; account: string; auth: string; folder: string; job_dir: string; state: string; step: string | null; progress: number | null;
  exit_code: number | null; email: string; error: string | null; params: any; outputs: string[]; parent: string | null; needs_password?: boolean;
  mail_error?: string | null; poll_error?: string | null; log_size?: number;
}

const KIND: Record<string, string> = { download: 'Download', bulk: 'Bulk', singlecell: 'Single cell' };
const active = (s: string) => s === 'starting' || s === 'running' || s === 'cancelling';
const elapsed = (j: Job) => fmtDuration((j.finished ?? Date.now() / 1000) - (j.started ?? j.created));

export function jobRow(j: Job, showOwner = false) {
  return `<tr data-job="${j.id}"><td><a href="/workbench/jobs/${j.id}/"><b>${esc(j.title)}</b></a><br><span class="muted small">${esc(KIND[j.kind] ?? j.kind)} · ${esc(j.server_name)}${j.account ? ` as ${esc(j.account)}` : ''}${showOwner ? ` · ${esc(j.owner)}` : ''}</span></td>
    <td>${stateChip(j.state)}${active(j.state) ? `<br><span class="small muted">${esc(j.step ?? '')}</span>` : ''}</td>
    <td>${j.progress != null && active(j.state) ? `<span class="wb-bar"><i style="width:${j.progress}%"></i></span>` : ''}</td>
    <td class="small">${fmtTime(j.created)}<br><span class="muted">${elapsed(j)}</span></td></tr>`;
}

export function jobsPage(root: HTMLElement) {
  let everyone = false;
  root.innerHTML = `<header class="wb-head"><h1>Jobs</h1><p class="muted">Every download and pipeline you started, wherever it runs. Jobs keep running when you close the page or the app.</p></header>
    <div class="wb-row" data-admin hidden><label class="wb-check"><input type="checkbox" data-everyone /> Everyone's jobs</label></div>
    <section class="card wb-card"><div class="table-wrap"><table class="table wb-table"><thead><tr><th>Job</th><th>State</th><th></th><th>Started</th></tr></thead><tbody data-list><tr><td colspan="4" class="muted">Loading…</td></tr></tbody></table></div></section>`;
  me().then((m) => { $('[data-admin]', root).hidden = !m.admin; });
  $('[data-everyone]', root).addEventListener('change', (e) => { everyone = (e.target as HTMLInputElement).checked; load(); });
  async function load() {
    try {
      const { jobs } = await wb<{ jobs: Job[] }>(`jobs${everyone ? '?all=1' : ''}`);
      $('[data-list]', root).innerHTML = jobs.map((j) => jobRow(j, everyone)).join('') || '<tr><td colspan="4" class="empty">No jobs yet: start one from Download, Bulk sequencing or Single cell.</td></tr>';
    } catch (err) { $('[data-list]', root).innerHTML = `<tr><td colspan="4" class="err">${esc((err as Error).message)}</td></tr>`; }
  }
  load();
  setInterval(load, 8000);
}

export function overviewPage(root: HTMLElement) {
  root.innerHTML = `<header class="wb-head"><h1>Omics Workbench</h1>
      <p class="muted">Analysis on the lab's servers: download sequencing data, run the bulk and single-cell pipelines, and follow them here, in the Mac app or by e-mail.</p></header>
    <div class="tools wb-tools">
      <a class="card tool" href="/workbench/download/"><h3>Download data</h3><p>Delivery e-mails (Novogene, MedGenome…), FTP / SFTP / HTTPS links, s3:// buckets, GEO and SRA accessions.</p></a>
      <a class="card tool" href="/workbench/bulk/"><h3>Bulk sequencing</h3><p>RNA-seq with DESeq2, ChIP-seq, CUT&amp;RUN and CUT&amp;Tag with MACS2 and controls, ATAC-seq.</p></a>
      <a class="card tool" href="/workbench/single-cell/"><h3>Single cell</h3><p>Cell Ranger GEX, ATAC and multiome; Seurat with reviewed QC, clustering and label transfer.</p></a>
    </div>
    <section class="card wb-card"><div class="wb-row"><h3>Your recent jobs</h3><span class="spacer"></span><a class="btn btn--quiet btn--sm" href="/workbench/jobs/">All jobs</a></div>
      <div class="table-wrap"><table class="table wb-table"><tbody data-jobs><tr><td class="muted">Loading…</td></tr></tbody></table></div></section>
    <section class="card wb-card"><div class="wb-row"><h3>Servers now</h3><span class="spacer"></span><span class="muted small">refreshed every 20 s</span></div>
      <div class="wb-servers" data-servers></div></section>`;
  async function load() {
    try {
      const [{ jobs }, { servers }] = await Promise.all([wb<{ jobs: Job[] }>('jobs'), loadServers()]);
      $('[data-jobs]', root).innerHTML = jobs.slice(0, 8).map((j) => jobRow(j)).join('') || '<tr><td class="empty">No jobs yet.</td></tr>';
      $('[data-servers]', root).innerHTML = servers.map((s) => serverCard(s)).join('');
    } catch (err) { toast(esc((err as Error).message)); }
  }
  load();
  setInterval(load, 20000);
}

// ------------------------------------------------------------------ one job

export function jobPage(root: HTMLElement, id: string) {
  let job: Job | null = null, offset = 0, follow = true, reviewShown = false;
  root.innerHTML = `<header class="wb-head wb-jobhead" data-head><h1>Job</h1></header>
    <section class="card wb-card" data-summary></section>
    <section class="card wb-card" data-review hidden></section>
    <section class="card wb-card wb-logcard"><div class="wb-row"><h3>Log</h3><span class="muted small" data-logsize></span><span class="spacer"></span>
      <label class="wb-check small"><input type="checkbox" data-follow checked /> Follow</label>
      <button type="button" class="btn btn--quiet btn--sm" data-reload>Reload</button></div>
      <pre class="wb-log" data-log></pre></section>`;
  const log = $('[data-log]', root);
  $('[data-follow]', root).addEventListener('change', (e) => { follow = (e.target as HTMLInputElement).checked; if (follow) log.scrollTop = log.scrollHeight; });
  $('[data-reload]', root).onclick = () => { offset = 0; log.textContent = ''; readLog(); };

  function summary(j: Job) {
    $('[data-head]', root).innerHTML = `<h1>${esc(j.title)}</h1><div class="wb-row">${stateChip(j.state)}<span class="muted">${esc(j.step ?? '')}</span>
      ${j.progress != null && active(j.state) ? `<span class="wb-bar wb-bar--wide"><i style="width:${j.progress}%"></i></span>` : ''}<span class="spacer"></span>
      ${active(j.state) ? '<button type="button" class="btn btn--danger btn--sm" data-cancel>Stop</button>' : '<button type="button" class="btn btn--quiet btn--sm" data-remove>Remove from the list</button>'}</div>`;
    const facts: [string, string][] = [
      ['Server', `${esc(j.server_name)} · ${j.account ? `account <span class="mono">${esc(j.account)}</span>` : 'service account (key)'}`],
      ['Output folder', `<span class="mono">${esc(j.folder)}</span> <button type="button" class="btn btn--quiet btn--xs" data-copy="${esc(j.folder)}">copy</button>`],
      ['Job folder', `<span class="mono small">${esc(j.job_dir)}</span> <span class="muted small">(run.sh, body.sh, log)</span>`],
      ['Started', `${fmtTime(j.started ?? j.created)} · ${elapsed(j)}${j.finished ? ` · ended ${fmtTime(j.finished)}` : ''}`],
    ];
    if (j.exit_code != null) facts.push(['Exit code', String(j.exit_code)]);
    if (j.email) facts.push(['E-mail', `${esc(j.email)}${j.mail_error ? ` <span class="err small">(not sent: ${esc(j.mail_error)})</span>` : ''}`]);
    if (j.parent) facts.push(['Continues', `<a href="/workbench/jobs/${esc(j.parent)}/">the QC job</a>`]);
    if (j.error) facts.push(['Error', `<span class="err">${esc(j.error)}</span>`]);
    if (j.poll_error) facts.push(['Last check', `<span class="err small">${esc(j.poll_error)}</span>`]);
    $('[data-summary]', root).innerHTML = `<dl class="wb-facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      ${j.needs_password ? `<form class="wb-row" data-unlock><span>This job runs with a password that this server no longer holds (it restarted). Give the password of <b>${esc(j.account)}</b> to follow it:</span>
        <input class="input" type="password" name="unlock" autocomplete="off" style="max-width:220px" /><button class="btn btn--primary btn--sm">Follow</button></form>` : ''}`;
  }

  root.addEventListener('click', async (ev) => {
    const t = ev.target as HTMLElement;
    const copy = t.closest<HTMLElement>('[data-copy]');
    if (copy) { navigator.clipboard?.writeText(copy.dataset.copy!).then(() => toast('Copied')); }
    if (t.closest('[data-cancel]') && job) {
      if (!confirm('Stop this job? Its process group is ended on the server; partial results stay in the folder.')) return;
      let password = '';
      try { await wb(`jobs/${id}/cancel`, {}); }
      catch (err) {
        if (!/password/i.test((err as Error).message)) return toast(esc((err as Error).message));
        password = prompt(`Password of ${job.account} on ${job.server_name} to stop the job:`) ?? '';
        if (!password) return;
        try { await wb(`jobs/${id}/cancel`, { password: await seal(password) }); } catch (e2) { return toast(esc((e2 as Error).message)); }
      }
      refresh();
    }
    if (t.closest('[data-remove]')) {
      if (!confirm('Remove this job from the list? Its files stay where they are.')) return;
      try { await wb(`jobs/${id}`, undefined, 'DELETE'); location.href = '/workbench/jobs/'; } catch (err) { toast(esc((err as Error).message)); }
    }
  });
  root.addEventListener('submit', async (ev) => {
    const form = (ev.target as HTMLElement).closest<HTMLFormElement>('[data-unlock]');
    if (!form) return;
    ev.preventDefault();
    try { await wb(`jobs/${id}/password`, { password: await seal(form.querySelector<HTMLInputElement>('input')!.value) }); refresh(); readLog(); }
    catch (err) { toast(esc((err as Error).message)); }
  });

  async function refresh() {
    try {
      job = (await wb<{ job: Job }>(`jobs/${id}`)).job;
      document.title = `${job.title} · Omics Workbench`;
      summary(job);
      if (!reviewShown && job.state === 'done' && job.kind === 'singlecell' && job.params?.seurat?.review && job.params.seurat.stage !== 'full') {
        reviewShown = true;
        review(job);
      }
    } catch (err) { $('[data-summary]', root).innerHTML = `<p class="err">${esc((err as Error).message)}</p>`; }
  }
  let reading = false;
  async function readLog() {
    if (reading) return;
    reading = true;
    try {
      for (let i = 0; i < 20; i++) {
        const r = await wb<{ text: string; offset: number; size: number }>(`jobs/${id}/log?offset=${offset}`);
        if (r.offset < offset) log.textContent = '';
        if (r.text) {
          const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
          log.append(document.createTextNode(r.text.replace(/\r(?!\n)/g, '\n')));
          if (log.textContent!.length > 2_000_000) log.textContent = log.textContent!.slice(-1_500_000);
          if (follow && atEnd) log.scrollTop = log.scrollHeight;
        }
        offset = r.offset;
        $('[data-logsize]', root).textContent = `${Math.round(r.size / 1024).toLocaleString()} KB`;
        if (offset >= r.size) break;
      }
    } catch (err) {
      if (!log.textContent) log.textContent = (err as Error).message;
    } finally { reading = false; }
  }

  async function review(j: Job) {
    const box = $('[data-review]', root);
    box.hidden = false;
    const out = j.params.seurat.output || `${j.params.output}/seurat`;
    const rel = out.startsWith(j.folder) ? out.slice(j.folder.length).replace(/^\//, '') : 'seurat';
    box.innerHTML = '<h3>Review the QC thresholds</h3><p class="muted">Loading the proposed thresholds…</p>';
    let rows: Record<string, string>[] = [];
    try {
      const res = await fetch(`/workbench/api/jobs/${j.id}/file?name=${encodeURIComponent(`${rel}/tables/qc_thresholds_proposed.tsv`)}`);
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error ?? 'The thresholds table was not found');
      const lines = (await res.text()).trim().split('\n');
      const head = lines[0].split('\t');
      rows = lines.slice(1).map((l) => Object.fromEntries(l.split('\t').map((v, i) => [head[i], v === 'NA' ? '' : v])));
    } catch (err) { box.innerHTML = `<h3>Review the QC thresholds</h3><p class="err">${esc((err as Error).message)}</p>`; return; }
    const cols = Object.keys(rows[0] ?? {});
    const editable = cols.filter((c) => /_(min|max)$/.test(c));
    box.innerHTML = `<h3>Review the QC thresholds</h3>
      <p class="muted">Proposed per sample from the median ± ${esc(String(j.params.seurat.nmad ?? 3))} MADs of log10 values, with floors and caps (as in the lab's multiome analysis);
        bounds are exclusive (percent.mt inclusive). Edit any value (empty: no bound), then continue: the filtering, clustering and label transfer run as a new job.</p>
      <div class="table-wrap"><table class="table wb-table wb-thresholds"><thead><tr>${cols.map((c) => `<th>${esc(c.replace(/_/g, ' '))}</th>`).join('')}</tr></thead><tbody>
        ${rows.map((r, i) => `<tr>${cols.map((c) => editable.includes(c) ? `<td><input class="input input--sm mono" data-row="${i}" data-col="${esc(c)}" value="${esc(r[c])}" /></td>`
          : `<td class="mono small">${esc(r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
      <div class="wb-figs">${rows.map((r) => `<figure><img loading="lazy" alt="QC of ${esc(r.sample_id)}" src="/workbench/api/jobs/${j.id}/file?name=${encodeURIComponent(`${rel}/figures/qc_${r.sample_id}.png`)}" /><figcaption>${esc(r.sample_id)}</figcaption></figure>`).join('')}</div>
      <div class="wb-row"><span class="muted small">${j.auth === 'password' ? `Runs again as <b>${esc(j.account)}</b> on ${esc(j.server_name)}:` : ''}</span>
        ${j.auth === 'password' ? '<input class="input" type="password" data-review-pw placeholder="password" style="max-width:200px" autocomplete="off" />' : ''}
        <span class="spacer"></span><button type="button" class="btn btn--primary" data-continue>Continue with these thresholds</button></div>`;
    box.addEventListener('change', (ev) => {
      const t = ev.target as HTMLInputElement;
      if (t.dataset.row) rows[+t.dataset.row][t.dataset.col!] = t.value.trim();
    });
    $('[data-continue]', box).onclick = async () => {
      const bad = rows.some((r) => editable.some((c) => r[c] !== '' && !/^-?\d+(\.\d+)?$/.test(r[c])));
      if (bad) return toast('Thresholds must be numbers (or empty)');
      try {
        const pw = box.querySelector<HTMLInputElement>('[data-review-pw]')?.value ?? '';
        const next = await wb<{ id: string }>('singlecell/continue', { job: j.id, thresholds: rows.map((r) => Object.fromEntries(cols.filter((c) => c !== 'cells_pass').map((c) => [c, r[c]]))),
          password: pw ? await seal(pw) : '', email: j.email });
        location.href = `/workbench/jobs/${next.id}/`;
      } catch (err) { toast(esc((err as Error).message)); }
    };
  }

  refresh().then(readLog);
  setInterval(async () => {
    if (job && !active(job.state) && offset >= (job.log_size ?? 0)) return;
    await refresh();
    await readLog();
  }, 3000);
}

export function bindFigureZoom(root: HTMLElement) {
  root.addEventListener('click', (ev) => {
    const img = (ev.target as HTMLElement).closest<HTMLImageElement>('.wb-figs img');
    if (!img) return;
    const dlg = document.createElement('dialog');
    dlg.className = 'wb-dialog wb-zoom';
    // the analysis writes each figure as PNG, SVG and PDF (vector) with the same name
    const name = (src: string, ext: string) => src.replace(/(name=[^&]*?)\.png(?=&|$)/, `$1.${ext}`);
    const file = decodeURIComponent((img.src.match(/name=([^&]*)/) ?? [])[1] ?? 'figure.png').split('/').pop()!.replace(/\.png$/, '');
    const links = ['png', 'svg', 'pdf'].map((ext) => `<a class="btn btn--light btn--sm" href="${esc(name(img.src, ext))}" download="${esc(`${file}.${ext}`)}">${ext.toUpperCase()}</a>`).join('');
    dlg.innerHTML = `<form method="dialog"><img src="${img.src}" alt="" /><footer>${links}<span class="spacer"></span><button class="btn btn--light">Close</button></footer></form>`;
    document.body.append(dlg);
    dlg.addEventListener('close', () => dlg.remove());
    dlg.showModal();
  });
  $$('img', root);
}
