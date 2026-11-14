// Omics Workbench pages: API calls, password encryption, the folder picker and the "Where to run" panel.

import { esc, fmtBytes } from '../common';

export { esc, fmtBytes };
export const $ = <T extends HTMLElement = HTMLElement>(sel: string, root: ParentNode = document) => root.querySelector<T>(sel)!;
export const $$ = <T extends HTMLElement = HTMLElement>(sel: string, root: ParentNode = document) => [...root.querySelectorAll<T>(sel)];

export async function wb<T = any>(path: string, body?: unknown, method?: string): Promise<T> {
  const res = await fetch(`/workbench/api/${path}`, {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { location.href = `/auth/login?next=${encodeURIComponent(location.pathname)}`; }
  if (!res.ok) throw new Error((data as { error?: string }).error ?? `Request failed (${res.status})`);
  return data as T;
}

export interface Me { user: string; admin: boolean; method: string; serviceUser: string; wrappers: string; setup: string; download: boolean }
let mePromise: Promise<Me> | null = null;
export const me = () => (mePromise ??= wb<Me>('me'));

// ------------------------------------------------------------------ passwords: RSA PKCS#1 v1.5 with BigInt, as the sign-in page
// (the site may be plain HTTP, where browsers offer no Web Crypto)

interface Key { n: string; e: string; bits: number }
let keyPromise: Promise<Key> | null = null;

function modPow(base: bigint, exponent: bigint, modulus: bigint) {
  let result = 1n;
  base %= modulus;
  while (exponent > 0n) {
    if (exponent & 1n) result = (result * base) % modulus;
    exponent >>= 1n;
    base = (base * base) % modulus;
  }
  return result;
}

/** "rsa:<base64>" of a password, for any field named "password" (the server decrypts it). */
export async function seal(text: string): Promise<string> {
  if (!text) return '';
  const key = await (keyPromise ??= wb<Key>('key'));
  const n = BigInt(`0x${key.n}`), e = BigInt(`0x${key.e}`), size = Math.ceil(key.bits / 8);
  const message = new TextEncoder().encode(text);
  if (message.length > size - 11) throw new Error('The password is too long');
  const padding = new Uint8Array(size - message.length - 3);
  crypto.getRandomValues(padding);
  for (let i = 0; i < padding.length; i++) while (padding[i] === 0) padding[i] = crypto.getRandomValues(new Uint8Array(1))[0];
  const block = new Uint8Array(size);
  block[1] = 2;
  block.set(padding, 2);
  block.set(message, padding.length + 3);
  let hex = '';
  for (const b of block) hex += b.toString(16).padStart(2, '0');
  const cipher = modPow(BigInt(`0x${hex}`), e, n).toString(16).padStart(size * 2, '0');
  let binary = '';
  for (let i = 0; i < cipher.length; i += 2) binary += String.fromCharCode(parseInt(cipher.slice(i, i + 2), 16));
  return `rsa:${btoa(binary)}`;
}

// ------------------------------------------------------------------ small things

export function toast(html: string, ms = 4500) {
  let box = document.querySelector<HTMLElement>('.wb-toast');
  if (!box) { box = document.createElement('div'); box.className = 'wb-toast'; document.body.append(box); }
  box.innerHTML = html;
  box.classList.add('show');
  clearTimeout((box as any)._t);
  (box as any)._t = setTimeout(() => box!.classList.remove('show'), ms);
}

export const fmtRate = (n?: number) => (n == null ? '–' : `${fmtBytes(Math.round(n))}/s`);
export const fmtTime = (s?: number | null) => (s ? new Date(s * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '–');
export const fmtDuration = (sec: number) => {
  if (!isFinite(sec) || sec < 0) return '–';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min` : `${Math.round(sec)} s`;
};
export const store = {
  get(key: string, fallback = '') { try { return localStorage.getItem(`workbench:${key}`) ?? fallback; } catch { return fallback; } },
  set(key: string, value: string) { try { localStorage.setItem(`workbench:${key}`, value); } catch { /* storage disabled */ } },
};

export const STATE_CHIP: Record<string, string> = {
  starting: 'chip--accent', running: 'chip--accent', cancelling: 'chip--warn', done: 'chip--ok', failed: 'chip--bad', cancelled: 'chip--warn',
};
export const stateChip = (s: string) => `<span class="chip ${STATE_CHIP[s] ?? ''}">${esc(s === 'done' ? 'finished' : s)}</span>`;

// ------------------------------------------------------------------ folder picker

interface Listing { path: string; parent: string | null; entries: { name: string; dir: boolean; size: number | null }[]; writable: boolean }

/** A dialog to choose a folder (or a file with files: true) on /nfs; resolves with the path or null. */
export function pickPath(start: string, options: { files?: boolean; title?: string; filter?: RegExp } = {}): Promise<string | null> {
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.className = 'wb-dialog wb-picker';
    dlg.innerHTML = `<form method="dialog" class="wb-picker__body">
      <header><h3>${esc(options.title ?? (options.files ? 'Choose a file' : 'Choose a folder'))}</h3><button class="icon-btn ghost sm" value="cancel" aria-label="Close">✕</button></header>
      <div class="wb-picker__bar"><button type="button" class="icon-btn sm" data-up title="Parent folder"><svg class="icon"><use href="#i-up"/></svg></button>
        <input class="input mono" data-path spellcheck="false" /><button type="button" class="btn btn--light btn--sm" data-go>Go</button></div>
      <ul class="files" data-list></ul>
      <footer><span class="muted small" data-note></span><span class="spacer"></span>
        <button class="btn btn--quiet" value="cancel">Cancel</button>
        <button type="button" class="btn btn--primary" data-choose>${options.files ? 'Choose file' : 'Choose this folder'}</button></footer>
    </form>`;
    document.body.append(dlg);
    let listing: Listing | null = null, picked = '';
    const pathInput = $<HTMLInputElement>('[data-path]', dlg);
    async function go(path: string) {
      try {
        listing = await wb<Listing>(`browse?path=${encodeURIComponent(path)}${options.files ? '&files=1' : ''}`);
      } catch (err) { toast(esc((err as Error).message)); return; }
      picked = '';
      pathInput.value = listing.path;
      $('[data-note]', dlg).textContent = options.files ? '' : listing.writable ? 'You can write here (as the service account)' : '';
      const entries = listing.entries.filter((e) => e.dir || !options.filter || options.filter.test(e.name));
      $('[data-list]', dlg).innerHTML = entries.map((e) => `<li class="file ${e.dir ? 'file--dir' : ''}" data-name="${esc(e.name)}" data-dir="${e.dir ? 1 : 0}">
        <svg class="icon file__icon"><use href="#i-${e.dir ? 'folder' : 'file'}"/></svg><span class="file__name">${esc(e.name)}</span>
        <span class="file__meta">${e.dir ? '' : fmtBytes(e.size)}</span></li>`).join('') || '<li class="muted pad">Empty folder</li>';
    }
    $('[data-list]', dlg).addEventListener('click', (ev) => {
      const li = (ev.target as HTMLElement).closest<HTMLElement>('li[data-name]');
      if (!li || !listing) return;
      const full = `${listing.path.replace(/\/$/, '')}/${li.dataset.name}`;
      if (li.dataset.dir === '1') go(full);
      else { picked = full; $$('li', dlg).forEach((x) => x.classList.toggle('sel', x === li)); }
    });
    $('[data-up]', dlg).onclick = () => listing?.parent && go(listing.parent);
    $('[data-go]', dlg).onclick = () => go(pathInput.value.trim());
    pathInput.onkeydown = (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); go(pathInput.value.trim()); } };
    $('[data-choose]', dlg).onclick = () => {
      const value = options.files ? picked : listing?.path ?? pathInput.value.trim();
      if (options.files && !value) { toast('Click a file first'); return; }
      dlg.close('ok');
      resolve(value || null);
    };
    dlg.addEventListener('close', () => { if (dlg.returnValue !== 'ok') resolve(null); dlg.remove(); });
    dlg.showModal();
    go(start || '/nfs');
  });
}

/** A text field with a Browse button. */
export function pathField(name: string, label: string, value = '', options: { files?: boolean; placeholder?: string; hint?: string; filter?: RegExp } = {}) {
  return `<label class="wb-field wb-field--path"><span>${esc(label)}</span>
    <span class="wb-path"><input class="input mono" name="${esc(name)}" value="${esc(value)}" placeholder="${esc(options.placeholder ?? '/nfs/…')}" spellcheck="false" />
    <button type="button" class="btn btn--light btn--sm" data-browse="${esc(name)}" data-files="${options.files ? 1 : 0}">Browse…</button></span>
    ${options.hint ? `<small class="muted">${options.hint}</small>` : ''}</label>`;
}

export function bindBrowse(root: HTMLElement, filters: Record<string, RegExp> = {}) {
  root.addEventListener('click', async (ev) => {
    const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-browse]');
    if (!b) return;
    const input = root.querySelector<HTMLInputElement>(`[name="${b.dataset.browse}"]`)!;
    const files = b.dataset.files === '1';
    const start = input.value.trim() ? (files ? input.value.replace(/\/[^/]*$/, '') : input.value.trim()) : store.get('lastFolder', '/nfs');
    const value = await pickPath(start, { files, filter: filters[b.dataset.browse!] });
    if (value) {
      input.value = value;
      store.set('lastFolder', files ? value.replace(/\/[^/]*$/, '') : value);
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
}

// ------------------------------------------------------------------ servers

export interface ServerRow {
  id: string; name: string; host: string; state: string; default: boolean; enabled: boolean; ncpu?: number; cpu?: number; iowait?: number;
  load?: number[]; mem_total?: number; mem_available?: number; disk_read?: number; disk_write?: number; net_rx?: number; net_tx?: number;
  users?: number; gpus?: number; free_cores?: number; error?: string; time?: number;
}

export const loadServers = () => wb<{ servers: ServerRow[]; interval: number }>('servers');

const bar = (pct: number, label: string) => {
  const p = Math.max(0, Math.min(100, pct || 0));
  return `<span class="wb-bar ${p > 85 ? 'hot' : p > 60 ? 'warm' : ''}" title="${esc(label)}"><i style="width:${p}%"></i></span>`;
};

export function serverCard(s: ServerRow, selected = false) {
  const mem = s.mem_total ? 100 * (1 - (s.mem_available ?? 0) / s.mem_total) : 0;
  const online = s.state === 'online';
  return `<button type="button" class="wb-server ${selected ? 'sel' : ''} ${online ? '' : 'off'}" data-server="${esc(s.id)}" ${s.enabled ? '' : 'disabled'}>
    <span class="wb-server__head"><b>${esc(s.name)}</b><span class="chip ${online ? 'chip--ok' : s.state === 'offline' ? 'chip--bad' : ''}">${esc(s.state)}</span></span>
    ${online ? `<span class="wb-server__row"><span>CPU</span>${bar(s.cpu!, `${s.cpu}% busy`)}<span class="mono">${Math.round(s.cpu!)}%</span></span>
      <span class="wb-server__row"><span>Memory</span>${bar(mem, `${fmtBytes(s.mem_available)} free`)}<span class="mono">${Math.round(mem)}%</span></span>
      <span class="wb-server__facts muted small">${s.ncpu} cores · load ${s.load![0].toFixed(1)} · ~${s.free_cores} free · ${fmtBytes(s.mem_available)} free${s.gpus ? ` · ${s.gpus} GPU` : ''} · ${s.users} logged in</span>`
      : `<span class="muted small">${esc(s.error ?? (s.state === 'unknown' ? 'checking…' : 'not monitored'))}</span>`}
  </button>`;
}

export interface RunChoice { server: string; account: string; password: string; email: string; setup: string }

/** "Where to run": server cards with their load, the account and its password, environment lines, e-mail. */
export class RunPanel {
  el: HTMLElement;
  selected = '';
  private timer = 0;
  /** fixed: always the default server (THOR), no server cards (downloads: the network, not the CPU, matters) */
  constructor(container: HTMLElement, private what = 'job', private fixed = false) {
    this.el = container;
    container.classList.add('wb-run');
    container.innerHTML = (fixed ? `<h3>Your account</h3>
      <p class="muted small">Runs on <b data-sname>THOR</b> and writes the files in your account there.</p>
      <div data-servers hidden></div>` : `<h3>Where to run</h3>
      <p class="muted small">Paths under <span class="mono">/nfs</span> are the same on every server; the check below verifies them from the chosen server.</p>
      <div class="wb-servers" data-servers><span class="muted">Loading servers…</span></div>`) + `
      <div class="wb-grid3">
        <label class="wb-field"><span>Account${fixed ? '' : ' on <b data-sname>the server</b>'}</span><input class="input" name="account" autocomplete="off" spellcheck="false" placeholder="your account" /></label>
        <label class="wb-field"><span>Password</span><input class="input" name="password" type="password" autocomplete="new-password" placeholder="kept in memory while the ${esc(what)} runs" /></label>
        <label class="wb-field"><span>E-mail when it ends <small class="muted">(optional)</small></span><input class="input" name="email" type="email" placeholder="you@stanford.edu" /></label>
      </div>
      <p class="muted small" data-keynote hidden></p>
      <details class="wb-adv"><summary>Environment lines</summary>
        <p class="muted small">Run before the ${esc(what)}, after the account's own <span class="mono">~/.bashrc</span> (e.g. <span class="mono">conda activate atac</span>, <span class="mono">module load …</span>).</p>
        <textarea class="input mono" name="setup" rows="3" spellcheck="false"></textarea></details>
      <div class="wb-checks" data-checks hidden></div>`;
    const email = $<HTMLInputElement>('[name=email]', container);
    email.value = store.get('email');
    email.onchange = () => store.set('email', email.value.trim());
    $<HTMLTextAreaElement>('[name=setup]', container).value = store.get('setup');
    $<HTMLTextAreaElement>('[name=setup]', container).onchange = (e) => store.set('setup', (e.target as HTMLTextAreaElement).value);
    const account = $<HTMLInputElement>('[name=account]', container);
    account.onchange = () => store.set(`account:${this.selected}`, account.value.trim());
    $('[data-servers]', container).addEventListener('click', (ev) => {
      const b = (ev.target as HTMLElement).closest<HTMLElement>('[data-server]');
      if (b) this.select(b.dataset.server!);
    });
    me().then((m) => {
      const note = $('[data-keynote]', container);
      note.hidden = !m.admin;
      note.innerHTML = `As an administrator you may leave account and password empty: the ${esc(what)} then runs in the server's own account (Admin → Servers) with this service's SSH key.`;
    });
    this.refresh();
    this.timer = window.setInterval(() => this.refresh(), 20000);
  }
  async refresh() {
    try {
      const { servers } = await loadServers();
      const fallback = servers.find((s) => s.default)?.id || servers[0]?.id || '';
      if (this.fixed) {
        this.selected = fallback;
        $('[data-sname]', this.el).textContent = servers.find((s) => s.id === fallback)?.name ?? 'THOR';
        $<HTMLInputElement>('[name=account]', this.el).value ||= store.get(`account:${fallback}`);
        clearInterval(this.timer);
        return;
      }
      if (!this.selected) this.selected = store.get('server') || fallback;
      $('[data-servers]', this.el).innerHTML = servers.map((s) => serverCard(s, s.id === this.selected)).join('');
      this.select(this.selected, false);
    } catch (err) { $('[data-servers]', this.el).innerHTML = `<span class="err">${esc((err as Error).message)}</span>`; }
  }
  select(id: string, remember = true) {
    const changed = id !== this.selected;
    this.selected = id;
    if (remember) store.set('server', id);
    $$('[data-server]', this.el).forEach((b) => b.classList.toggle('sel', b.dataset.server === id));
    const name = this.el.querySelector(`[data-server="${CSS.escape(id)}"] b`)?.textContent ?? id;
    $('[data-sname]', this.el).textContent = name;
    if (changed || remember) {
      $<HTMLInputElement>('[name=account]', this.el).value = store.get(`account:${id}`);
      $<HTMLInputElement>('[name=password]', this.el).value = '';
    }
  }
  async choice(): Promise<RunChoice> {
    const v = (n: string) => $<HTMLInputElement>(`[name=${n}]`, this.el).value.trim();
    if (!this.selected) throw new Error('Choose a server');
    return { server: this.selected, account: v('account'), password: await seal($<HTMLInputElement>('[name=password]', this.el).value),
      email: v('email'), setup: $<HTMLTextAreaElement>('[name=setup]', this.el).value };
  }
  showChecks(r: { as: string; results: { what: string; path: string; ok: boolean; detail: string }[]; ok: boolean }) {
    const box = $('[data-checks]', this.el);
    box.hidden = false;
    const bad = r.results.filter((x) => !x.ok);
    box.innerHTML = `<p><b>${r.ok ? '✓ Ready' : '✕ Problems'}</b> — checked as <span class="mono">${esc(r.as)}</span>${bad.length ? `: ${bad.length} of ${r.results.length} not found` : `: ${r.results.length} paths and programs found`}</p>
      <ul class="wb-checklist">${r.results.map((x) => `<li class="${x.ok ? 'ok' : 'bad'}"><span>${x.ok ? '✓' : '✕'}</span><b>${esc(x.what)}</b>
        <span class="mono">${esc(x.path)}</span><span class="muted">${esc(x.detail)}</span></li>`).join('')}</ul>`;
    box.classList.toggle('bad', !r.ok);
  }
  destroy() { clearInterval(this.timer); }
}

/** A dialog showing a script. */
export function showScript(text: string, title = 'Job script') {
  const dlg = document.createElement('dialog');
  dlg.className = 'wb-dialog';
  dlg.innerHTML = `<form method="dialog"><header><h3>${esc(title)}</h3><button class="icon-btn ghost sm" aria-label="Close">✕</button></header>
    <pre class="wb-log wb-script">${esc(text)}</pre><footer><span class="muted small">Runs under <span class="mono">bash -Eeuo pipefail</span> in the job's folder wrapper.</span><span class="spacer"></span><button class="btn btn--light">Close</button></footer></form>`;
  document.body.append(dlg);
  dlg.addEventListener('close', () => dlg.remove());
  dlg.showModal();
}
