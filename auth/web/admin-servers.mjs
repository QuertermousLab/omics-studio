// The admin page's Servers and Workbench tabs: the lab servers' load (Omics Workbench's monitor, /workbench/api/servers/…),
// their list and SSH keys, and the Workbench settings (pipelines folder, environment lines, e-mail, s3 buckets).

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const bytes = (n) => {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${units[i]}`;
};
const rate = (n) => `${bytes(n)}/s`;
const ago = (t) => (t ? `${Math.max(0, Math.round(Date.now() / 1000 - t))} s ago` : "–");
const duration = (s) => (s > 86400 ? `${Math.floor(s / 86400)} d` : s > 3600 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 60)} min`);

async function call(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

// passwords are encrypted with the Workbench's RSA key (the site may be plain HTTP), as on the sign-in page
let keyPromise = null;
function modPow(base, exponent, modulus) {
  let result = 1n;
  base %= modulus;
  while (exponent > 0n) {
    if (exponent & 1n) result = (result * base) % modulus;
    exponent >>= 1n;
    base = (base * base) % modulus;
  }
  return result;
}
async function seal(text) {
  const key = await (keyPromise ??= call("/workbench/api/key"));
  const n = BigInt(`0x${key.n}`), e = BigInt(`0x${key.e}`), size = Math.ceil(key.bits / 8);
  const message = new TextEncoder().encode(text);
  const padding = new Uint8Array(size - message.length - 3);
  crypto.getRandomValues(padding);
  for (let i = 0; i < padding.length; i += 1) while (padding[i] === 0) padding[i] = crypto.getRandomValues(new Uint8Array(1))[0];
  const block = new Uint8Array(size);
  block[1] = 2;
  block.set(padding, 2);
  block.set(message, padding.length + 3);
  let hex = "";
  for (const b of block) hex += b.toString(16).padStart(2, "0");
  const cipher = modPow(BigInt(`0x${hex}`), e, n).toString(16).padStart(size * 2, "0");
  let binary = "";
  for (let i = 0; i < cipher.length; i += 2) binary += String.fromCharCode(parseInt(cipher.slice(i, i + 2), 16));
  return `rsa:${btoa(binary)}`;
}

// ---------------------------------------------------------------- charts (inline SVG, theme colors)

function sparkline(history, index, max, label, format = (v) => v.toFixed(0)) {
  const values = history.map((h) => h[index]);
  if (values.length < 2) return `<div class="sv-spark"><span>${label}</span><small>collecting…</small></div>`;
  const top = max ?? Math.max(...values, 1);
  const w = 220, h = 38;
  const points = values.map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${(h - (Math.min(v, top) / top) * (h - 2) - 1).toFixed(1)}`).join(" ");
  const last = values[values.length - 1];
  return `<div class="sv-spark"><span>${label} <b>${format(last)}</b></span>
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(label)} over the last ${Math.round((history.at(-1)[0] - history[0][0]) / 60)} minutes">
      <polyline points="0,${h} ${points} ${w},${h}" class="sv-area"/><polyline points="${points}" class="sv-line"/></svg></div>`;
}

const meter = (pct, label) => {
  const p = Math.max(0, Math.min(100, pct || 0));
  return `<span class="sv-meter ${p > 85 ? "hot" : p > 60 ? "warm" : ""}" title="${esc(label)}"><i style="width:${p}%"></i></span>`;
};

function coreGrid(cores) {
  return `<div class="sv-cores" title="Each square is a CPU core (darker = busier)">${cores.map((c) => `<i style="--v:${Math.min(100, c) / 100}" title="${c}%"></i>`).join("")}</div>`;
}

// ---------------------------------------------------------------- the Servers tab

let details = [];
let interval = 20;

function card(entry) {
  const s = entry.server, r = entry.latest, h = entry.history;
  if (!s.enabled) return `<div class="ad-card sv-card off"><h2>${esc(s.name)} <span class="ad-state disabled">disabled</span></h2><p class="ad-muted">${esc(s.user)}@${esc(s.host)}</p></div>`;
  if (!r || !r.ok) {
    return `<div class="ad-card sv-card off" data-sid="${esc(s.id)}"><h2>${esc(s.name)} <span class="ad-state ${r ? "failed" : ""}">${r ? "offline" : "checking"}</span></h2>
      <p class="ad-muted">${esc(s.user)}@${esc(s.host)}:${s.port}${r ? ` · ${ago(r.time)}` : ""}</p>${r ? `<p class="ad-danger">${esc(r.error)}</p>` : ""}
      ${keyForm(s)}</div>`;
  }
  const d = r.data;
  const mem = 100 * (1 - d.mem_available / d.mem_total);
  const swap = d.swap_total ? 100 * (1 - d.swap_free / d.swap_total) : 0;
  return `<div class="ad-card sv-card" data-sid="${esc(s.id)}">
    <div class="sv-head"><h2>${esc(s.name)} <span class="ad-state active">online</span></h2>
      <span class="ad-muted">${esc(d.hostname)} · ${esc(s.user)}@${esc(s.host)} · up ${duration(d.uptime)} · ${ago(r.time)} (${r.latency} s)</span>
      <button class="quiet-button small" data-refresh="${esc(s.id)}">Refresh</button></div>
    <p class="ad-muted">${esc(d.cpu_model)} · ${d.ncpu} cores · ${bytes(d.mem_total)} memory${d.gpus.length ? ` · ${d.gpus.length} GPU` : ""}</p>
    <div class="sv-now">
      <div><span>CPU</span>${meter(d.cpu, `${d.cpu}%`)}<b>${d.cpu.toFixed(0)}%</b><small>iowait ${d.iowait}%</small></div>
      <div><span>Load</span>${meter((100 * d.load[0]) / d.ncpu, "")}<b>${d.load[0].toFixed(1)}</b><small>${d.load[1].toFixed(1)} · ${d.load[2].toFixed(1)}</small></div>
      <div><span>Memory</span>${meter(mem, "")}<b>${mem.toFixed(0)}%</b><small>${bytes(d.mem_available)} free</small></div>
      <div><span>Swap</span>${meter(swap, "")}<b>${swap.toFixed(0)}%</b><small>${bytes(d.swap_total - d.swap_free)} of ${bytes(d.swap_total)}</small></div>
      <div><span>Disk</span><b>${rate(d.disk_read)}</b><small>read · ${rate(d.disk_write)} write</small></div>
      <div><span>Network</span><b>${rate(d.net_rx)}</b><small>in · ${rate(d.net_tx)} out</small></div>
    </div>
    ${coreGrid(d.cores)}
    <div class="sv-sparks">${sparkline(h, 1, 100, "CPU %")}${sparkline(h, 2, 100, "Memory %")}${sparkline(h, 3, d.ncpu, "Load")}
      ${sparkline(h, 4, null, "Disk read", rate)}${sparkline(h, 5, null, "Disk write", rate)}${sparkline(h, 6, null, "Net in", rate)}${sparkline(h, 7, null, "Net out", rate)}</div>
    <div class="sv-cols">
      <div><h3>Logged in (${d.users.length})</h3>${d.users.length ? `<table class="ad-table"><tbody>${d.users.map((u) => `<tr><td><b>${esc(u.user)}</b></td><td class="num">${u.sessions}</td><td><small>${esc(u.from.join(", "))}</small></td></tr>`).join("")}</tbody></table>` : '<p class="ad-muted">nobody</p>'}</div>
      <div><h3>Accounts using the server</h3><table class="ad-table"><thead><tr><th>Account</th><th class="num">CPU</th><th class="num">Memory</th><th class="num">Procs</th></tr></thead><tbody>
        ${d.accounts.map((a) => `<tr><td>${esc(a.user)}</td><td class="num">${a.cpu.toFixed(0)}%</td><td class="num">${bytes(a.rss)}</td><td class="num">${a.procs}</td></tr>`).join("") || '<tr><td colspan="4" class="ad-muted">idle</td></tr>'}</tbody></table></div>
    </div>
    <details class="sv-more"><summary>Processes, GPUs and disks</summary>
      <table class="ad-table"><thead><tr><th>Account</th><th class="num">PID</th><th class="num">CPU</th><th class="num">Memory</th><th class="num">Running</th><th>Command</th></tr></thead><tbody>
        ${d.processes.map((p) => `<tr><td>${esc(p.user)}</td><td class="num">${p.pid}</td><td class="num">${p.cpu.toFixed(0)}%</td><td class="num">${bytes(p.rss)}</td><td class="num">${duration(p.elapsed)}</td><td><code>${esc(p.command)}</code></td></tr>`).join("") || '<tr><td colspan="6" class="ad-muted">no busy processes</td></tr>'}</tbody></table>
      ${d.gpus.length ? `<table class="ad-table"><thead><tr><th>GPU</th><th class="num">Use</th><th class="num">Memory</th><th class="num">°C</th></tr></thead><tbody>${d.gpus.map((g) => `<tr><td>${g.index}: ${esc(g.name)}</td><td class="num">${g.util}%</td><td class="num">${bytes(g.mem_used)} / ${bytes(g.mem_total)}</td><td class="num">${g.temp}</td></tr>`).join("")}</tbody></table>` : ""}
      <table class="ad-table"><thead><tr><th>Mount</th><th>Type</th><th class="num">Size</th><th class="num">Free</th><th></th></tr></thead><tbody>
        ${d.filesystems.map((f) => `<tr><td><code>${esc(f.mount)}</code></td><td>${esc(f.kind)}</td><td class="num">${bytes(f.size)}</td><td class="num">${bytes(f.free)}</td><td>${meter(100 * (1 - f.free / f.size), "")}</td></tr>`).join("")}</tbody></table>
    </details></div>`;
}

function keyForm(s) {
  return `<form class="ad-row" data-key="${esc(s.id)}"><span class="ad-muted">Install this server's SSH key for <b>${esc(s.user || "the account")}</b> (the password is used once and not kept):</span>
    <label>Account <input type="text" name="account" value="${esc(s.user)}" autocomplete="off"></label>
    <label>Password <input type="password" name="password" autocomplete="off" required></label>
    <button class="secondary-button small" type="submit">Install key</button><span class="ad-message"></span></form>`;
}

function serverTable(list) {
  return `<table class="ad-table" id="sv-list"><thead><tr><th>Name</th><th>Host</th><th class="num">Port</th><th>Account</th><th>Monitored</th><th>Note</th><th></th></tr></thead><tbody>
    ${list.map((s) => `<tr><td><input name="name" value="${esc(s.name)}" size="10"></td><td><input name="host" value="${esc(s.host)}" size="16"></td>
      <td class="num"><input name="port" type="number" value="${s.port}" style="width:70px"></td><td><input name="user" value="${esc(s.user)}" size="10"></td>
      <td><input name="enabled" type="checkbox" ${s.enabled ? "checked" : ""}></td><td><input name="note" value="${esc(s.note)}" size="18"></td>
      <td><button class="quiet-button small ad-danger" type="button" data-remove-row>Remove</button></td></tr>`).join("")}</tbody></table>`;
}

async function loadServers() {
  const data = await call("/workbench/api/servers/details");
  details = data.servers;
  interval = data.interval;
  $("#sv-cards").innerHTML = details.map(card).join("");
  if (!$("#sv-list")) {
    $("#sv-list-wrap").innerHTML = serverTable(details.map((d) => d.server));
    $("#sv-service").textContent = data.serviceUser;
  }
}

function listValues() {
  return $$("#sv-list tbody tr").map((row) => ({
    id: $('[name="name"]', row).value.trim().toLowerCase(), name: $('[name="name"]', row).value.trim(), host: $('[name="host"]', row).value.trim(),
    port: Number($('[name="port"]', row).value) || 22, user: $('[name="user"]', row).value.trim(), enabled: $('[name="enabled"]', row).checked,
    note: $('[name="note"]', row).value.trim(),
  })).filter((s) => s.name && s.host);
}

// ---------------------------------------------------------------- the Workbench tab

async function loadWorkbench() {
  const s = await call("/workbench/api/settings");
  const f = $("#wb-settings");
  f.wrappers.value = s.wrappers || "";
  f.setup.value = s.setup || "";
  f.method.value = s.mail.method || "sendmail";
  f.from.value = s.mail.from || "";
  f.host.value = s.mail.host || "";
  f.port.value = s.mail.port || 25;
  f.starttls.checked = !!s.mail.starttls;
  f.user.value = s.mail.user || "";
  f.password.value = s.mail.password || "";
  f.server.value = s.mail.server || "";
  f.buckets.value = Object.entries(s.buckets || {}).map(([b, c]) => `${b} ${c.profile} ${c.region}`).join("\n");
  mailFields();
}
function mailFields() {
  const method = $("#wb-settings").method.value;
  for (const el of $$("[data-mail]")) el.hidden = !el.dataset.mail.split(" ").includes(method);
}

export function initServers({ onSelect }) {
  onSelect("servers", () => loadServers().catch((e) => { $("#sv-cards").innerHTML = `<p class="ad-danger">${esc(e.message)} — is the Workbench service running (./vizhubctl status)?</p>`; }));
  onSelect("workbench", () => loadWorkbench().catch((e) => { $("#wb-message").textContent = e.message; }));
  setInterval(() => { if (!$('[data-panel="servers"]').hidden) void loadServers().catch(() => {}); }, 10000);

  $("#sv-add").addEventListener("click", () => {
    $("#sv-list tbody").insertAdjacentHTML("beforeend", `<tr><td><input name="name" size="10"></td><td><input name="host" size="16"></td><td class="num"><input name="port" type="number" value="22" style="width:70px"></td>
      <td><input name="user" size="10"></td><td><input name="enabled" type="checkbox" checked></td><td><input name="note" size="18"></td><td><button class="quiet-button small ad-danger" type="button" data-remove-row>Remove</button></td></tr>`);
  });
  $("#sv-list-wrap").addEventListener("click", (event) => { if (event.target.closest("[data-remove-row]")) event.target.closest("tr").remove(); });
  $("#sv-save").addEventListener("click", async () => {
    try {
      await call("/workbench/api/servers", { method: "POST", body: JSON.stringify({ servers: listValues() }) });
      $("#sv-message").textContent = `Saved ${new Date().toLocaleTimeString()}; checking the servers…`;
      $("#sv-list-wrap").innerHTML = "";
      setTimeout(() => loadServers().catch(() => {}), 2500);
    } catch (error) { $("#sv-message").textContent = error.message; }
  });
  $("#sv-cards").addEventListener("click", async (event) => {
    const b = event.target.closest("[data-refresh]");
    if (!b) return;
    b.disabled = true;
    try { await call(`/workbench/api/servers/${b.dataset.refresh}/refresh`, { method: "POST", body: "{}" }); await loadServers(); } catch (error) { alert(error.message); }
    b.disabled = false;
  });
  $("#sv-cards").addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-key]");
    if (!form) return;
    event.preventDefault();
    const message = $(".ad-message", form);
    message.textContent = "Installing…";
    try {
      const r = await call(`/workbench/api/servers/${form.dataset.key}/key`, { method: "POST",
        body: JSON.stringify({ account: form.account.value.trim(), password: await seal(form.password.value) }) });
      message.textContent = r.ok ? "Installed: the key works" : "Installed, but the key login still fails";
      setTimeout(() => loadServers().catch(() => {}), 3000);
    } catch (error) { message.textContent = error.message; }
  });

  const f = $("#wb-settings");
  f.method.addEventListener("change", mailFields);
  f.addEventListener("submit", async (event) => {
    event.preventDefault();
    const buckets = {};
    for (const line of f.buckets.value.split("\n")) {
      const [bucket, profile, region] = line.trim().split(/\s+/);
      if (bucket) buckets[bucket.replace(/^s3:\/\//, "")] = { profile: profile || "default", region: region || "" };
    }
    try {
      await call("/workbench/api/settings", { method: "POST", body: JSON.stringify({
        wrappers: f.wrappers.value.trim(), setup: f.setup.value, buckets,
        mail: { method: f.method.value, from: f.from.value.trim(), host: f.host.value.trim(), port: Number(f.port.value) || 25, starttls: f.starttls.checked,
          user: f.user.value.trim(), password: f.password.value && f.password.value !== "(set)" ? await seal(f.password.value) : f.password.value,
          server: f.server.value.trim() } }) });
      $("#wb-message").textContent = `Saved ${new Date().toLocaleTimeString()}`;
    } catch (error) { $("#wb-message").textContent = error.message; }
  });
  $("#wb-mail-test").addEventListener("click", async () => {
    const to = prompt("Send a test e-mail to:");
    if (!to) return;
    $("#wb-message").textContent = "Sending…";
    try { await call("/workbench/api/mail-test", { method: "POST", body: JSON.stringify({ to }) }); $("#wb-message").textContent = `Sent to ${to} (check the inbox and the spam folder)`; }
    catch (error) { $("#wb-message").textContent = `Not sent: ${error.message}`; }
  });
}
