import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, key } from './dom.mjs';

const LONG = 'Nothing changed: WEBUX: land_on release/0.4 would split 3 waits across targets: '
  + 'WEBUX-NODES waits on CSSFRESH-REBUILD (main), WEBUX-DECIDE waits on WEBUX-NODES-STEP (main)';
const LINE = 16;

function tooltip(page) {
  const tip = page.$('#tm-tooltip');
  return tip.classList.contains('hidden') ? null : tip;
}

// The test DOM lays nothing out, so the toast text reports the height it would take: three
// lines once clamped, and `lines` of content.
function withTextHeight(page, lines, fn) {
  const proto = Object.getPrototypeOf(page.document.createElement('span'));
  Object.defineProperty(proto, 'scrollHeight', { configurable: true, get: () => lines * LINE });
  Object.defineProperty(proto, 'clientHeight', { configurable: true, get: () => Math.min(lines, 3) * LINE });
  try {
    return fn();
  } finally {
    delete proto.scrollHeight;
    delete proto.clientHeight;
  }
}

test('a refusal past three lines is cut there, and its full text is the tooltip on hover and on keyboard focus', () => {
  const page = loadPage();
  withTextHeight(page, 5, () => page.run(`toast(${JSON.stringify(LONG)}, { tone: 'error', retry: () => {} })`));
  const text = page.$('#toast-root .toast-message');
  assert.ok(text.className.split(/\s+/).includes('line-clamp-3'));
  assert.equal(text.getAttribute('tabindex'), '0');
  assert.equal(text.getAttribute('aria-label'), null, 'its name stays its own text');

  text.dispatchEvent(new page.window.Event('mouseover'));
  assert.equal(tooltip(page).textContent, LONG);
  text.dispatchEvent(new page.window.Event('mouseout'));
  assert.equal(tooltip(page), null);

  text.focus();
  text.dispatchEvent(new page.window.Event('focusin'));
  assert.equal(tooltip(page).textContent, LONG);
  text.dispatchEvent(new page.window.Event('focusout'));
  assert.equal(tooltip(page), null);
});

test("Escape closes a refusal's open tooltip first, and the toast on the next Escape", () => {
  const page = loadPage();
  withTextHeight(page, 5, () => page.run(`toast(${JSON.stringify(LONG)}, { tone: 'error' })`));
  const text = page.$('#toast-root .toast-message');
  text.focus();
  text.dispatchEvent(new page.window.Event('focusin'));
  key(text, 'Escape');
  assert.equal(tooltip(page), null);
  assert.ok(page.$('#toast-root .toast'), 'the toast stays for the next Escape');
  key(text, 'Escape');
  assert.equal(page.$('#toast-root .toast'), null);
});

test('a refusal that fits in three lines is no tab stop and has no tooltip', () => {
  const page = loadPage();
  withTextHeight(page, 3, () => page.run("toast('Nothing changed: refused', { tone: 'error' })"));
  const text = page.$('#toast-root .toast-message');
  assert.equal(text.getAttribute('tabindex'), null);
  assert.equal(text.getAttribute('data-tip'), null);
  text.dispatchEvent(new page.window.Event('mouseover'));
  assert.equal(tooltip(page), null);
});

test('a tooltip with no room below its anchor opens above it', () => {
  const page = loadPage();
  withTextHeight(page, 5, () => page.run(`toast(${JSON.stringify(LONG)}, { tone: 'error' })`));
  const text = page.$('#toast-root .toast-message');
  const tip = page.$('#tm-tooltip');
  tip.getBoundingClientRect = () => ({ top: 0, left: 0, right: 320, bottom: 60, width: 320, height: 60 });

  text.getBoundingClientRect = () => ({ top: 818, left: 1052, right: 1360, bottom: 866, width: 308, height: 48 });
  text.dispatchEvent(new page.window.Event('mouseover'));
  assert.equal(tip.style.top, `${818 - 60 - 6}px`);
  text.dispatchEvent(new page.window.Event('mouseout'));

  text.getBoundingClientRect = () => ({ top: 100, left: 1052, right: 1360, bottom: 148, width: 308, height: 48 });
  text.dispatchEvent(new page.window.Event('mouseover'));
  assert.equal(tip.style.top, `${148 + 6}px`);
});

test("a centred dialog's panel ends above the tallest toast, so Cancel and Save stay clear of a refusal", () => {
  // The toast sits 16px off the bottom and is 66px tall at three lines.
  const TOAST_BAND = 16 + 3 * LINE + 18;
  const page = loadPage();
  page.$('#new-menu-btn').click();
  page.$('[data-new-kind="spec"]').click();
  const panel = page.$('#dialog-root [role="dialog"]');
  const cap = panel.className.match(/(?:^|\s)max-h-\[calc\(100vh-(\d+)px\)\](?:\s|$)/);
  assert.ok(cap, `the panel's height is capped against the viewport: ${panel.className}`);
  assert.ok(Number(cap[1]) / 2 >= TOAST_BAND, `${cap[1]}px off a centred panel leaves the toast's ${TOAST_BAND}px clear`);
});
