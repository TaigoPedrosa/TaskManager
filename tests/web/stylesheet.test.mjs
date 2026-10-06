import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync, spawnSync } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.join(here, '../..');
const COMMITTED_CSS = path.join(REPO_ROOT, 'src/taskmanager/web/static/tailwind.css');
const INPUT_CSS = path.join(REPO_ROOT, 'src/taskmanager/web/static/tailwind.input.css');
const CONFIG = path.join(REPO_ROOT, 'tailwind.config.js');

// The standalone CLI ships as one binary per platform (see README, "Rebuilding the web
// stylesheet"); it is never installed as a Node package, so it is looked up on PATH or at the
// repo-root path the README's fetch command writes it to, never assumed present.
function findTailwindCli() {
  const candidates = [process.env.TAILWINDCSS_BIN, path.join(REPO_ROOT, 'tailwindcss')].filter(
    Boolean,
  );
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  try {
    const finder = process.platform === 'win32' ? 'where' : 'which';
    const found = execFileSync(finder, ['tailwindcss'], { encoding: 'utf8' }).trim().split('\n')[0];
    if (found) return found;
  } catch {
    // not on PATH
  }
  return null;
}

// A missing CLI fails rather than skips, so no gate passes without comparing the sheet to its build.
test('the committed stylesheet matches a fresh build from tailwind.config.js', () => {
  const cli = findTailwindCli();
  assert.ok(
    cli,
    'tailwindcss standalone CLI not found: fetch it as README "Rebuilding the web stylesheet" ' +
      'shows, then set TAILWINDCSS_BIN to its path or put it on PATH',
  );
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-tailwind-'));
  try {
    const outFile = path.join(outDir, 'tailwind.css');
    execFileSync(cli, ['-i', INPUT_CSS, '-c', CONFIG, '-o', outFile], {
      cwd: REPO_ROOT,
      stdio: ['ignore', 'ignore', 'inherit'],
    });
    const fresh = fs.readFileSync(outFile, 'utf8');
    const committed = fs.readFileSync(COMMITTED_CSS, 'utf8');
    assert.equal(fresh, committed, 'tailwind.css is stale: rebuild it (see README) and commit the result');
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

// The README's fetch writes the binary to the repository root. A scratch repository holding only
// the project's .gitignore keeps the check off the developer's global excludes, and works when
// the tree under test is an export with no .git of its own.
test('the tailwindcss binary at the repository root is git-ignored', () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-gitignore-'));
  try {
    execFileSync('git', ['init', '-q'], { cwd: repo });
    fs.copyFileSync(path.join(REPO_ROOT, '.gitignore'), path.join(repo, '.gitignore'));
    const args = ['-c', 'core.excludesFile=/dev/null', 'check-ignore', '-q', 'tailwindcss'];
    const check = spawnSync('git', args, { cwd: repo });
    assert.equal(check.status, 0, '.gitignore does not ignore /tailwindcss, so `git add -A` commits the CLI');
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
