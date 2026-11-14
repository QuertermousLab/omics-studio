// Omics Workbench (/workbench/…): one page, the view chosen by the address. Served by workbench/server.py.

import { initChrome, postToDesktop } from './common';
import { bulkPage } from './workbench/bulk';
import { downloadPage } from './workbench/download';
import { bindFigureZoom, jobPage, jobsPage, overviewPage } from './workbench/jobs';
import { singleCellPage } from './workbench/singlecell';

const VIEWS: [string, string, string][] = [
  ['overview', 'Overview', '/workbench/'],
  ['download', 'Download data', '/workbench/download/'],
  ['bulk', 'Bulk sequencing', '/workbench/bulk/'],
  ['single-cell', 'Single cell', '/workbench/single-cell/'],
  ['jobs', 'Jobs', '/workbench/jobs/'],
];

initChrome('workbench');
const path = location.pathname;
const jobId = /^\/workbench\/jobs\/([a-z0-9]{12})\/?$/.exec(path)?.[1];
const view = jobId ? 'jobs' : (VIEWS.find(([, , href]) => href !== '/workbench/' && path.startsWith(href))?.[0] ?? 'overview');
const nav = document.querySelector<HTMLElement>('[data-wb-nav]')!;
nav.innerHTML = VIEWS.map(([id, label, href]) => `<a href="${href}" class="${id === view ? 'sel' : ''}" ${id === view ? 'aria-current="page"' : ''}>${label}</a>`).join('');
const root = document.querySelector<HTMLElement>('[data-wb-root]')!;
const title = VIEWS.find((v) => v[0] === view)![1];
document.title = view === 'overview' ? 'Omics Workbench' : `${title} · Omics Workbench`;
if (jobId) jobPage(root, jobId);
else if (view === 'download') downloadPage(root);
else if (view === 'bulk') bulkPage(root);
else if (view === 'single-cell') singleCellPage(root);
else if (view === 'jobs') jobsPage(root);
else overviewPage(root);
bindFigureZoom(root);
postToDesktop({ type: 'workbench', view, job: jobId ?? null });
