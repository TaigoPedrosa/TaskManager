import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './dom.mjs';

const BEAT = new Date(Date.now() - 8000).toISOString();

const ROW = {
  id: 'T', kind: 'task', title: 'A merging task', parent: null, status: 'MERGING', display: 'MERGING', phase: 'DISPATCHED',
  ordinal: 0, priority: 50, child_count: 0, acceptable_models: [], target_repo: 'svc', lease: null, review: true, fix: true, merge: 'spec',
};

const BODY = {
  node: {
    ...ROW, review_cycles: 0, merge_attempts: 0, step_failures: 0, requires: [], land_order: [], base_chain: ['MAIN'], frontmatter: {},
  },
  display: 'MERGING', phase: 'DISPATCHED',
  sections: [], dependency_details: [], dependent_details: [], verifications: [], conditions: [], jobs: [],
  findings: [], checks: [], notes: [], lease: null,
  steps: [{
    action: 'merge', agent: 'merger', started: BEAT, ended: null, outcome: null, to: null, round: null,
    items: [{ kind: 'job parked', job: 'land-<b>1</b>', reason: 'conflict' }],
  }],
};

function fakeVis(w) {
  class DataSet {
    constructor(items) { this.items = new Map(items.map((i) => [i.id, i])); }
    getIds() { return [...this.items.keys()]; }
    update(list) { list.forEach((i) => this.items.set(i.id, i)); }
    remove(ids) { ids.forEach((id) => this.items.delete(id)); }
  }
  class Network {
    constructor() { this.handlers = {}; }
    on(event, fn) { (this.handlers[event] ||= []).push(fn); }
    getBoundingBox() { return { left: 0, top: 0, right: 0, bottom: 0 }; }
    canvasToDOM(p) { return p; }
    fit() {}
    selectNodes() {}
    unselectAll() {}
    focus() {}
  }
  w.vis = { DataSet, Network };
}

test('a job parked item reads as a sentence naming the job and its reason, escaped', async () => {
  const page = loadPage({
    url: 'file:///tmp/export.html#/graph/T',
    beforeScripts: (w) => {
      w.STATIC_DATA = { rows: [ROW], edges: [], statuses: [], bodies: { T: BODY }, decisions: [] };
      fakeVis(w);
    },
  });
  await page.settle();

  const item = page.$('#inspector-body [data-group="steps"] [data-step] [data-step-item="job parked"]');
  assert.equal(item.textContent.trim(), 'job land-<b>1</b> parked: conflict');
  assert.equal(page.$('#inspector-body [data-group="steps"] b'), null);
});
