import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { loadPage } from './dom.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.join(here, '../..');

// The page under test is the one `tm web export` writes, over a freshly initialized project,
// so the inline scripts, their order and the injected globals are exactly what ships. A grep
// of the sources passes on a page whose scripts throw before ever reaching the checked call.
function exportedPage() {
  const project = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-static-export-'));
  // tm runs with uv and git alone on its PATH, so a codegraph installed on this machine is
  // never run by `tm init`.
  const bin = path.join(project, 'bin');
  fs.mkdirSync(bin);
  for (const tool of ['uv', 'git']) {
    const found = process.env.PATH.split(path.delimiter).map((dir) => path.join(dir, tool)).find((p) => fs.existsSync(p));
    fs.symlinkSync(found, path.join(bin, tool));
  }
  try {
    const tm = (...args) => execFileSync('uv', ['run', '--project', REPO_ROOT, '--quiet', 'tm', ...args], {
      stdio: ['ignore', 'ignore', 'inherit'],
      env: { ...process.env, PATH: bin },
    });
    execFileSync('git', ['-C', project, 'init', '-q']);
    tm('init', '-C', project);
    const out = path.join(project, 'export.html');
    tm('web', 'export', '-C', project, '-o', out);
    return fs.readFileSync(out, 'utf8');
  } finally {
    fs.rmSync(project, { recursive: true, force: true });
  }
}

const EXPORTED = exportedPage();

test('a static export runs every inline script without throwing, opens on Document, and offers no Waves view', async () => {
  const page = loadPage({ html: EXPORTED, url: 'file:///tmp/export.html' });
  await page.settle();
  assert.equal(page.$('#document-pane').classList.contains('hidden'), false);
  assert.equal(page.$('#graph-pane').classList.contains('hidden'), true);
  assert.equal(page.$('#waves-pane').classList.contains('hidden'), true);
  assert.equal(page.$('#view-waves-btn'), null, 'the Waves segment is gone');
  assert.equal(page.$('#waves-content').innerHTML, '', 'no Waves message is rendered');
  assert.equal(page.fetchCalls.length, 0, 'nothing is fetched from a file');
  // The three segments the static data supports stay, Decisions with its badge.
  for (const id of ['view-doc-btn', 'view-graph-btn', 'view-decisions-btn']) assert.ok(page.$(`#${id}`), `${id} stays`);
  assert.ok(page.$('#view-decisions-btn #decisions-badge'));
  assert.match(page.$('#unified-document').innerHTML, /No specs, plans or tasks match/, 'the Document view rendered from the embedded data');
});

test('a static export opened at #/decisions lands on the Decisions view, and #/waves falls back to Document', async () => {
  const page = loadPage({ html: EXPORTED, url: 'file:///tmp/export.html#/decisions' });
  await page.settle();
  assert.equal(page.$('#decisions-pane').classList.contains('hidden'), false);
  assert.equal(page.$('#document-pane').classList.contains('hidden'), true);
  assert.ok(page.$('#view-decisions-btn').classList.contains('bg-zinc-800'));
  assert.equal(page.window.location.hash, '#/decisions');

  const waves = loadPage({ html: EXPORTED, url: 'file:///tmp/export.html#/waves' });
  await waves.settle();
  assert.equal(waves.$('#document-pane').classList.contains('hidden'), false);
  assert.equal(waves.window.location.hash, '#/document');
});
