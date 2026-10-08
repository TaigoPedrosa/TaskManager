import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { loadPage, jsonResponse } from './dom.mjs';

const REPO_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '../..');
const OLD = 'S-P-OLD';
const NEW = 'S-P-NEW';

// A scratch estate read by the server's own rows, bodies and counts: OLD superseded by NEW,
// with NEW in `status`.
function estate(status) {
  const script = `
import json, sys, tempfile
from pathlib import Path
from taskmanager.core.status import Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations
from taskmanager.engine.snapshot import display_view
from taskmanager.web.bodies import BodyRepos, build_bodies
from taskmanager.web.rows import build_rows, statuses

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    container = create_container(root)
    container.get(DatabaseManager).init_all()
    ops = container.get(Operations)
    plan = ops.add_plan("A plan", ops.add_spec("A spec", slug="S"), slug="P")
    ops.supersede(ops.add_task("First try", plan, slug="OLD"), ops.add_task("Shipped", plan, slug="NEW"))
    repo = ops.node_repo
    repo.save_node(repo.get_node("${NEW}").model_copy(update={"status": Status(sys.argv[1])}))
    view = display_view(repo)
    rows = build_rows(view)
    repos = BodyRepos(repo, JobRepository(repo.db), CacheRepository(repo.db), 0, root)
    print(json.dumps({"rows": rows, "bodies": build_bodies(view, list(rows), repos=repos), "statuses": statuses(rows)}, default=str))
`;
  return JSON.parse(execFileSync('uv', ['run', '--project', REPO_ROOT, '--quiet', 'python', '-c', script, status], { encoding: 'utf8' }));
}

async function openAt(url, data) {
  const fetch = async (u) => {
    const parsed = new URL(u, 'http://test');
    const ids = parsed.searchParams.get('ids');
    if (parsed.pathname === '/api/nodes' && ids) return jsonResponse(200, { items: ids.split(',').map((i) => data.rows[i]).filter(Boolean), next: null });
    const m = parsed.pathname.match(/^\/api\/nodes\/([^/]+)$/);
    if (m) return jsonResponse(200, data.bodies[decodeURIComponent(m[1])]);
    return undefined;
  };
  const page = loadPage({ url, fetch });
  await page.settle();
  page.socket.open();
  const subscribe = page.socket.sent.at(-1);
  page.socket.message({
    type: 'snapshot', re: subscribe && subscribe.id, rows: Object.values(data.rows),
    bodies: data.bodies, statuses: data.statuses, edges: [], decisions_open: 0,
  });
  await page.settle();
  return page;
}

const text = (el) => el.textContent.replace(/\s+/g, ' ').trim();

const COMPLETED = estate('COMPLETED');

for (const [surface, url, scope] of [['Document card', `/document/${OLD}`, `#doc-node-${OLD}`], ['drawer', `/graph/${OLD}`, '#inspector-body']]) {
  test(`the ${surface} opens with "Superseded by <id> (<status>)", the id linked to the node`, async () => {
    const page = await openAt(url, COMPLETED);
    const detail = page.$(`${scope} [data-detail-for="${OLD}"]`);
    const line = detail.children[0];
    assert.ok(line.classList.contains('superseded-by'), 'the line comes before the facts strip');
    assert.equal(text(line), `Superseded by ${NEW} (Completed)`);
    const link = line.querySelector(`a.id-link[data-id="${NEW}"]`);
    assert.equal(link.getAttribute('href'), `/${url.split('/')[1]}/${NEW}`);
    assert.equal(line.querySelector('[role="img"]'), null, 'the status is spelled out, so no status icon sits beside it');
  });
}

test('a node nothing replaced has no superseded line', async () => {
  const page = await openAt(`/document/${NEW}`, COMPLETED);
  assert.ok(page.$(`#doc-node-${NEW} [data-detail-for="${NEW}"]`));
  assert.equal(page.$(`#doc-node-${NEW} .superseded-by`), null);
});

test('a superseded node counts under Completed once its replacement completed, and is set aside until then', async () => {
  const done = await openAt(`/document/${OLD}`, COMPLETED);
  assert.equal(done.$('#doc-node-S-P .plan-line div[role="img"]').getAttribute('aria-label'), '2 completed');

  const pending = await openAt(`/document/${OLD}`, estate('READY'));
  assert.equal(pending.$('#doc-node-S-P .plan-line div[role="img"]').getAttribute('aria-label'), '1 ready · 1 superseded');
  assert.equal(text(pending.$(`#doc-node-${OLD} .superseded-by`)), `Superseded by ${NEW} (Ready)`);
});
