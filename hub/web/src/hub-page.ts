// The home page: tool cards in groups, with the services' status (Genome Canvas, GREAT, Omics Workbench) and each viewer's dataset counts
// (yours, shared in the lab).
import { api, initChrome, loadWorkspace, onWorkspace } from './common';

initChrome('hub');

// the services' cards (Genome Canvas, GREAT): online / offline, from the hub's health checks
api<{ id: string; ok: boolean }[]>('/hub/api/services').then((list) => {
  for (const el of document.querySelectorAll<HTMLElement>('[data-tool] [data-state]')) {
    const tool = el.closest<HTMLElement>('[data-tool]')!.dataset.tool;
    const service = list.find((x) => x.id === tool);
    const show = (ok: boolean) => { el.textContent = ok ? 'online' : 'offline'; el.className = `chip ${ok ? 'chip--ok' : 'chip--bad'}`; };
    if (service) show(service.ok);
    // a service the hub does not check yet (added after it started): ask it directly
    else fetch(`/${tool}/api/health`).then((r) => show(r.ok)).catch(() => show(false));
  }
}).catch(() => {});

async function counts() {
  const s = await api<{ kinds: Record<string, { mine: number; all: number; shared?: number }>; running: number; workspace: { name: string } | null }>('/hub/api/summary');
  for (const [k, n] of Object.entries(s.kinds)) {
    const el = document.querySelector(`[data-tool="${k}"] [data-count]`);
    if (!el) continue;
    const own = s.workspace ? (n.shared ? `${n.mine} yours` : `${n.mine} in your workspace`) : `${n.all} dataset${n.all === 1 ? '' : 's'}`;
    el.textContent = n.shared ? `${own} · ${n.shared} shared` : own;
    el.className = `chip ${(s.workspace ? n.mine : n.all) ? 'chip--accent' : ''}`;
  }
}
onWorkspace(() => counts().catch(() => {}));
loadWorkspace();
