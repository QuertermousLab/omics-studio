// AlphaGenome control page: follows /alphagenome/studio-api/status, starts and stops the model on the GPU server.
const API = "/alphagenome/studio-api/";
const THEME_KEY = "genome-canvas:theme";
const $ = (id) => document.getElementById(id);

// ---- theme, shared with every Omics Studio page
const system = window.matchMedia("(prefers-color-scheme: dark)");
const saved = () => { try { return localStorage.getItem(THEME_KEY); } catch { return null; } };
const applyTheme = (theme) => {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#2e3440" : "#f8f9fb";
};
applyTheme(saved() || (system.matches ? "dark" : "light"));
system.addEventListener("change", () => { if (!saved()) applyTheme(system.matches ? "dark" : "light"); });
window.addEventListener("storage", (e) => { if (e.key === THEME_KEY) applyTheme(e.newValue || (system.matches ? "dark" : "light")); });
$("theme-toggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try {
    if (theme === (system.matches ? "dark" : "light")) localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch { /* storage disabled */ }
  applyTheme(theme);
});

// ---- status
const LABELS = { ready: "Ready", starting: "Starting…", stopping: "Stopping…", stopped: "Stopped", unknown: "Unreachable" };
let last = null;
let wantOpen = new URLSearchParams(location.search).has("open");
let busy = false;
let unreachable = false;

const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
};
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function message(text, info = false) {
  const el = $("message");
  el.hidden = !text;
  el.textContent = text || "";
  el.classList.toggle("info", info);
}

function render(s) {
  last = s;
  $("server-name").textContent = s.name || "the GPU server";
  const chip = $("phase-chip");
  chip.className = "chip " + s.phase;
  chip.textContent = LABELS[s.phase] || s.phase;
  if (s.idleMinutes) $("idle-text").textContent = s.idleMinutes >= 60 && s.idleMinutes % 60 === 0
    ? `${s.idleMinutes / 60} hour${s.idleMinutes === 60 ? "" : "s"}` : `${s.idleMinutes} minutes`;

  const ready = s.phase === "ready" && s.tunnel;
  $("start").hidden = !(s.phase === "stopped");
  $("start").disabled = busy;
  $("open").hidden = !ready;
  $("stop").hidden = !(s.phase === "ready" || s.phase === "starting");
  $("stop").disabled = busy;
  $("stop").className = "secondary-button" + (s.othersActive.length || s.modelUsers.length ? " danger" : "");
  $("action-hint").textContent = s.phase === "starting" ? "Loading the model on the GPUs…"
    : s.phase === "ready" && !s.tunnel ? "Connecting…" : "";

  // an unreachable server's message goes when it answers again; messages of Start / Stop stay until the next one
  if (s.phase === "unknown") { message(s.error.startsWith(s.name) ? s.error : `Can't reach ${s.name}: ${s.error}`); unreachable = true; }
  else if (unreachable) { message(""); unreachable = false; }
  if (s.phase === "ready" && $("message").textContent.startsWith("Starting")) message("");

  const facts = [];
  if (s.host) facts.push(["Server", `${esc(s.name)} <span style="color:var(--text-4)">(${esc(s.host)})</span>`]);
  if (s.startedBy && s.startedAt) facts.push(["Started", `by ${esc(s.startedBy)} at ${clock(s.startedAt)}`]);
  else if (s.phase === "ready") facts.push(["Started", "outside this page (it is not stopped automatically)"]);
  if (s.lastUsed && s.phase === "ready") facts.push(["Last prediction", ago(s.lastUsed)]);
  if (s.idleStopAt) facts.push(["Stops by itself", `at ${clock(s.idleStopAt)} unless it is used`]);
  if (s.recentUsers.length) facts.push(["Used by", s.recentUsers.map((u) => `${esc(u.user)} (${ago(u.t)})`).join(", ")]);
  if (s.modelUsers.length) facts.push(["Also using the model", s.modelUsers.map((u) => esc(u.what) + (u.count > 1 ? ` ×${u.count}` : "")).join(", ")]);
  const lastEvent = s.events[s.events.length - 1];
  if (lastEvent && s.phase === "stopped") facts.push(["Last", `${esc(lastEvent.what)} by ${esc(lastEvent.user || "the page")}, ${ago(lastEvent.t)}`]);
  $("facts").innerHTML = facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  $("gpus").innerHTML = (s.gpus || []).map((g) => {
    const pct = g.total ? Math.round(100 * g.used / g.total) : 0;
    return `<div class="gpu"><div class="name"><span>GPU ${g.index} · ${esc(g.name)}</span><span>${g.util ?? "–"}% busy</span></div>
      <div class="bar"><span style="width:${pct}%"></span></div>
      <div class="mem">${(g.used / 1024).toFixed(1)} / ${(g.total / 1024).toFixed(1)} GB</div></div>`;
  }).join("");

  const log = (s.startLog || "").trim();
  $("log-box").hidden = !(log && s.phase !== "ready");
  $("log").textContent = log;
  if (s.phase === "starting") $("log-box").open = true;

  if (ready && wantOpen) location.href = "/alphagenome/";
}

async function refresh() {
  try {
    const r = await fetch(API + "status", { cache: "no-store" });
    if (r.status === 401) { location.href = "/auth/login?next=" + encodeURIComponent(location.pathname); return; }
    render(await r.json());
  } catch (error) {
    message("Omics Studio is not answering: " + error.message);
  }
}

async function post(route, body) {
  busy = true;
  if (last) render(last);
  try {
    const r = await fetch(API + route, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    return await r.json();
  } finally {
    busy = false;
  }
}

$("start").addEventListener("click", async () => {
  message("Starting AlphaGenome on " + (last?.name || "the GPU server") + "…", true);
  wantOpen = true;
  const out = await post("start");
  if (!out.ok) { wantOpen = false; message(out.error || "Could not start"); }
  refresh();
});

$("stop").addEventListener("click", async () => {
  const s = last || {};
  let force = false;
  if (s.othersActive?.length || s.modelUsers?.length) {
    if (!s.admin) {
      message(s.othersActive.length
        ? `${s.othersActive.join(", ")} used AlphaGenome in the last 10 minutes. Ask them or an administrator to stop it.`
        : `Other programs are using the model (${s.modelUsers.map((u) => u.what).join(", ")}). Only an administrator can stop it.`);
      return;
    }
    if (!confirm("Others are using AlphaGenome right now. Stop it anyway? Their predictions will fail.")) return;
    force = s.modelUsers.length > 0;
  } else if (!confirm("Stop AlphaGenome and free the GPUs? Open Atlas pages stop working until someone starts it again.")) {
    return;
  }
  wantOpen = false;
  message("Stopping…", true);
  const out = await post("stop", { force });
  message(out.ok ? "Stopped: the GPUs are free." : (out.error || "Could not stop"), out.ok);
  refresh();
});

refresh();
setInterval(refresh, 4000);
