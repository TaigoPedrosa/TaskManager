import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { discovery, djb2, json, landedRepo, makeTm, meta, realCksum, runWave, scriptCksum } from './harness.mjs'

const ARGS = { session: 's1', worktreeDir: '/wt', root: '/est' }
const T1 = { id: 'T1', kind: 'task', action: 'implement', model: 'sonnet', repos: ['core'], requires: [], job: null, migration: false }

const node = (status, next_action, extra = {}) => ({ id: 'T1', kind: 'task', status, next_action, outcome: null, ...extra })
const claim = (action, extra = {}) =>
  json({ action, reason: null, model: 'sonnet', job: null, repos: ['core'], branch: 'tm/T1', base: 'main', worktree: null, worktrees: {}, token: 'k1', ...extra })
const jobAt = (state, extra = {}) =>
  json({
    id: 'J1', kind: 'land', node_id: 'T1', repo: 'core', target: 'main', state, step: 'gate',
    worktree: '/est/.worktrees/land-T1', pid: 4242, heartbeat: null, result: null, ...extra,
  })
const starts = ops => ops.filter(c => / task start /.test(c))
const releases = ops => ops.filter(c => / task release /.test(c))
const gets = ops => ops.filter(c => / task get /.test(c))
const discoverCmd = ops => ops[0].match(/^out=\$\((.*) 2>&1\); rc=/)[1]
// A release the script makes without having read a claim can name only the agent.
const RELEASE_T1 = 'tm task release T1 --agent wf-s1-T1 >/dev/null 2>&1'
const releaseOf = token => `tm task release T1 --agent wf-s1-T1 --token ${token} >/dev/null 2>&1`
const REVIEWERS = { task: 'task-reviewer', rereview: 'scoped-re-reviewer', container: 'branch-reviewer' }
const scopeOf = work => work.prompt.split('\n').find(line => line.startsWith('Scope: '))

test('session and worktreeDir are required', async () => {
  await assert.rejects(runWave({ args: { session: 's1' }, tm: makeTm() }), /args\.session and args\.worktreeDir are required/)
})

for (const key of ['release', 'maxFixRounds']) {
  test(`a caller passing ${key} is told what replaced it`, async () => {
    await assert.rejects(runWave({ args: { ...ARGS, [key]: [] }, tm: makeTm() }), new RegExp(`args\\.${key} is no longer read`))
  })
}

test('discovery asks about every spec unless specs names some', async () => {
  const all = await runWave({ args: ARGS, tm: makeTm() })
  const some = await runWave({ args: { ...ARGS, specs: ['S1', 'S2'] }, tm: makeTm() })
  assert.equal(discoverCmd(all.ops), 'tm wave discover --lines --session s1 --slots 9 --max-strong 5')
  assert.equal(discoverCmd(some.ops), 'tm wave discover --lines --spec S1 --spec S2 --session s1 --slots 9 --max-strong 5')
})

test('discovery is told which merges to hold, so a held merge never takes a slot', async () => {
  const { ops } = await runWave({ args: { ...ARGS, holdMerge: ['T1', 'T2'] }, tm: makeTm() })
  assert.equal(discoverCmd(ops), 'tm wave discover --lines --session s1 --slots 9 --max-strong 5 --hold-merge T1 --hold-merge T2')
})

test('a discovery transcript whose cksum never matches claims nothing', async () => {
  const tm = makeTm({ chosen: [T1], corrupt: inner => /wave discover/.test(inner) })
  await assert.rejects(runWave({ args: ARGS, tm }), /discovery failed three times/)
})

test('a discovery line of an unknown shape claims nothing', async () => {
  const tm = makeTm({ discover: () => 'N T1 implement sonnet task core\nW 0' })
  await assert.rejects(runWave({ args: ARGS, tm }), /discovery failed three times/)
})

test('discovery lines carry kind, repos and requires through to the claim', async () => {
  const tm = makeTm({ chosen: [{ ...T1, kind: 'plan', repos: ['api', 'web'], requires: ['figma'] }], nodes: { T1: node('COMPLETED', null) } })
  const { logs, errors } = await runWave({ args: ARGS, tm })
  assert.deepEqual(errors, [])
  assert.ok(logs.some(l => l.startsWith('wave: T1@implement/sonnet')))
})

test('a payload carrying non-ASCII text passes its checksum', async () => {
  const tm = makeTm({ discover: () => discovery([], { held: ['T9: aguardando a decisão D1'] }) })
  const { logs, errors } = await runWave({ args: ARGS, tm })
  assert.deepEqual(errors, [])
  assert.ok(logs.includes('held: T9: aguardando a decisão D1'))
})

test('a READY task takes one step per run: implement, then review, then merge', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: {
      T1: [
        () => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' })),
        () => (tm.set('T1', { status: 'REVIEWING', next_action: null }), claim('review')),
        () => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' })),
      ],
    },
    job: { J1: [() => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded', { step: 'complete' }))] },
  })
  const agents = (prompt, opts) => {
    if (opts.label === 'implement:T1') tm.set('T1', { status: 'IMPLEMENTED', next_action: 'review' })
    if (opts.label === 'review:T1') tm.set('T1', { status: 'REVIEWED', next_action: 'merge', outcome: 'approve' })
    return 'done'
  }

  const implementTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(implementTick.errors, [])
  assert.equal(implementTick.result.results[0].status, 'IMPLEMENTED')
  assert.deepEqual(implementTick.work.map(w => w.opts.label), ['implement:T1'])
  assert.equal(starts(implementTick.ops).length, 1)
  assert.equal(gets(implementTick.ops).length, 2, 'one read before the claim and one after the step')

  const reviewTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(reviewTick.errors, [])
  assert.equal(reviewTick.result.results[0].status, 'REVIEWED')
  assert.deepEqual(reviewTick.work.map(w => w.opts.label), ['review:T1'])
  assert.equal(starts(reviewTick.ops).length, 1)

  const mergeTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(mergeTick.errors, [])
  assert.equal(mergeTick.result.results[0].status, 'COMPLETED')
  assert.deepEqual(mergeTick.work, [])
  assert.equal(starts(mergeTick.ops).length, 1)

  assert.deepEqual(releases([...implementTick.ops, ...reviewTick.ops, ...mergeTick.ops]), [])
})

test('read() names exactly the fields it uses, and never sees the rest', async () => {
  // A poisoned row: fields run()/work() do not read, set to values that would misroute the
  // tick if the fake (or the real CLI) leaked them past --fields.
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: { ...node('READY', 'implement'), verdict: 'reject', branch: 'tm/other', jobs: ['poison'] } },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')
  const { ops, result } = await runWave({ args: ARGS, tm, agents })
  assert.match(gets(ops)[0], /tm task get T1 --json --fields status,next_action,outcome /)
  assert.equal(result.results[0].status, 'IMPLEMENTED')
})

test('every agent runs under a phase the meta declares, across the run each step takes', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: {
      T1: [
        () => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' })),
        () => (tm.set('T1', { status: 'REVIEWING', next_action: null }), claim('review')),
        () => (tm.set('T1', { status: 'FIXING', next_action: null }), claim('fix', { worktree: '/wt/core-T1' })),
        () => (tm.set('T1', { status: 'REVIEWING', next_action: null }), claim('review')),
        () => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' })),
        () => (tm.set('T1', { next_action: null }), claim('merge', { job: 'J1', token: 'k2' })),
      ],
    },
    job: {
      J1: [
        () => (tm.set('T1', { next_action: 'merge' }), jobAt('needs_agent', { step: 'build', result: 'conflict' })),
        jobAt('needs_agent', { step: 'build', result: 'conflict' }),
        jobAt('running'),
        () => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded')),
      ],
    },
  })
  let reviews = 0
  const agents = (prompt, opts) => {
    if (opts.label === 'merge-agent:T1') tm.resume('J1')
    if (opts.label === 'implement:T1') tm.set('T1', { status: 'IMPLEMENTED', next_action: 'review' })
    if (opts.label === 'review:T1') {
      reviews += 1
      tm.set('T1', reviews === 1 ? { status: 'REVIEWED', next_action: 'fix', outcome: 'reject' } : { status: 'REVIEWED', next_action: 'merge', outcome: 'approve' })
    }
    if (opts.label === 'fix:T1') tm.set('T1', { status: 'FIXED', next_action: 'review' })
    return 'done'
  }
  const ticks = []
  for (let i = 0; i < 6; i++) ticks.push(await runWave({ args: ARGS, tm, agents }))
  const calls = ticks.flatMap(t => t.calls)
  const work = ticks.flatMap(t => t.work)
  const errors = ticks.flatMap(t => t.errors)
  const titles = meta().phases.map(p => p.title)
  assert.deepEqual(errors, [])
  assert.deepEqual(work.map(w => w.opts.label), ['implement:T1', 'review:T1', 'fix:T1', 'review:T1', 'merge-agent:T1'])
  for (const call of calls) assert.ok(titles.includes(call.opts.phase), `${call.opts.label} runs under ${call.opts.phase}`)
  assert.equal(ticks[4].work.length, 0, 'the run whose job stops for an agent dispatches nobody')
  assert.equal(ticks[4].result.results[0].status, 'MERGING')
  assert.equal(ticks[5].result.results[0].status, 'COMPLETED')
})

for (const [fam, models, id] of [
  ['opus', undefined, 'claude-opus-5'],
  ['fable', undefined, 'claude-fable-5-1'],
  ['opus', { opus: 'claude-opus-6' }, 'claude-opus-6'],
]) {
  test(`a step tm routes to the ${fam} family runs on it and names ${id}`, async () => {
    const tm = makeTm({
      chosen: [T1],
      nodes: { T1: node('READY', 'implement') },
      start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { model: fam, worktree: '/wt/core-T1' }))] },
    })
    const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')
    const { work, result } = await runWave({ args: { ...ARGS, ...(models ? { models } : {}) }, tm, agents })
    assert.equal(work[0].opts.model, fam)
    assert.match(work[0].prompt, new RegExp(`^Model: ${id}$`, 'm'))
    assert.ok(result.results[0].trail.includes(`implement on ${fam}: done`))
  })
}

test('a claim naming a family args.models does not map is released and never dispatched', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { model: 'claude-opus-5', worktree: '/wt/core-T1' }))] },
  })
  const { ops, work, result } = await runWave({ args: ARGS, tm })
  assert.deepEqual(work, [])
  assert.deepEqual(releases(ops), [releaseOf('k1')])
  assert.ok(result.results[0].trail.includes('claim names the model family claude-opus-5, which args.models does not map'))
})

test('an implement brief names the worktree, the branch, its base and the verb that closes it', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')
  const { work } = await runWave({ args: { ...ARGS, agentTypes: { core: 'python-dev' } }, tm, agents })
  for (const text of ['tm-task: T1', 'tm guide implement', 'Worktree: /wt/core-T1', 'branch tm/T1', 'based on main', 'tm task complete T1 --agent wf-s1-T1 --token k1', 'tm task release T1 --agent wf-s1-T1 --token k1 --blocked', 'tm render T1 --view subagent']) {
    assert.ok(work[0].prompt.includes(text), text)
  }
  assert.equal(work[0].opts.agentType, 'python-dev')
})

test('a step the agent leaves open is released as a counted failure', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const { ops, result } = await runWave({ args: ARGS, tm, agents: () => 'I stopped before committing' })
  assert.deepEqual(releases(ops), [releaseOf('k1')])
  assert.ok(result.results[0].trail.includes('released, the implement step was left open'))
})

test("a dead agent's step is released", async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const { ops, result } = await runWave({ args: ARGS, tm, agents: () => null })
  assert.deepEqual(releases(ops), [releaseOf('k1')])
  assert.ok(result.results[0].trail.includes('implement on sonnet: the agent died'))
})

test('a step the agent closed is never released by the script', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'READY', next_action: null }), 'released --blocked on T2')
  const { ops } = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(releases(ops), [])
})

test("a step already claimed by another dispatcher's next claim is not released", async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'REVIEWING', next_action: null }), 'done')
  const { ops } = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(releases(ops), [])
})

test("a blocked claim ends the loop with tm's reason and dispatches nobody", async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [json({ action: 'blocked', reason: 'waits on the decisão D1', job: null }, 3)] },
  })
  const { result, work } = await runWave({ args: ARGS, tm })
  assert.deepEqual(work, [])
  assert.equal(result.results[0].status, 'blocked')
  assert.ok(result.results[0].trail.includes('blocked: waits on the decisão D1'))
})

test('a claim whose transcription fails its checksum is released and never acted on', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [claim('implement', { worktree: '/wt/core-T1' })] },
    corrupt: inner => / task start /.test(inner),
  })
  const { ops, work } = await runWave({ args: ARGS, tm })
  assert.equal(starts(ops).length, 1)
  assert.deepEqual(work, [])
  assert.deepEqual(releases(ops), [RELEASE_T1])
})

test("the script's cksum agrees with the stock cksum binary", () => {
  for (const text of ['', 'aguardando a decisão D1 — não é assim', 'x'.repeat(70_000)]) {
    assert.equal(scriptCksum(text), realCksum(text))
  }
})

test('a check line shaped like a djb2 rather than a cksum is rejected', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [claim('implement', { worktree: '/wt/core-T1' })] },
    corrupt: (inner, text) => (/ task start /.test(inner) ? `${djb2(text)} ${text.length}` : false),
  })
  const { ops, work } = await runWave({ args: ARGS, tm })
  assert.equal(starts(ops).length, 1)
  assert.deepEqual(work, [])
  assert.deepEqual(releases(ops), [RELEASE_T1])
})

test('a check line hashed over a payload one byte off is rejected', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [claim('implement', { worktree: '/wt/core-T1' })] },
    corrupt: (inner, text) => (/ task start /.test(inner) ? realCksum(`${text} `) : false),
  })
  const { ops, work } = await runWave({ args: ARGS, tm })
  assert.equal(starts(ops).length, 1)
  assert.deepEqual(work, [])
  assert.deepEqual(releases(ops), [RELEASE_T1])
})

test('a claim that exits 0 but prints no readable step is released, not left to its lease', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [{ text: 'Warning: config key deprecated\naction: implement', exit: 0 }] },
  })
  const { ops, work, result } = await runWave({ args: ARGS, tm })
  assert.deepEqual(work, [])
  assert.deepEqual(releases(ops), [RELEASE_T1])
  assert.ok(result.results[0].trail.some(t => t.startsWith('released, its claim printed no step')))
})

test('holdMerge stops a node before its merge claim', async () => {
  const tm = makeTm({ chosen: [{ ...T1, action: 'merge' }], nodes: { T1: node('REVIEWED', 'merge', { outcome: 'approve' }) } })
  const { ops, result } = await runWave({ args: { ...ARGS, holdMerge: ['T1'] }, tm })
  assert.deepEqual(starts(ops), [])
  assert.ok(result.results[0].trail.includes('merge held by args.holdMerge'))
})

test('holdMerge does not stop the steps before the merge', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: 'merge' }), 'done')
  const { ops, work } = await runWave({ args: { ...ARGS, holdMerge: ['T1'] }, tm, agents })
  assert.deepEqual(work.map(w => w.opts.label), ['implement:T1'])
  assert.equal(starts(ops).length, 1)
})

test('a node at FAILED is not claimed', async () => {
  const tm = makeTm({ chosen: [T1], nodes: { T1: node('FAILED', null) } })
  const { ops, result } = await runWave({ args: ARGS, tm })
  assert.deepEqual(starts(ops), [])
  assert.equal(result.results[0].status, 'FAILED')
})

test('a landing its own claim started is handed to an agent only by a later claim', async () => {
  const tm = makeTm({
    chosen: [{ ...T1, action: 'merge' }],
    nodes: { T1: node('REVIEWED', 'merge', { outcome: 'approve' }) },
    start: {
      T1: [
        () => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' })),
        () => (tm.set('T1', { next_action: null }), claim('merge', { job: 'J1', token: 'k2' })),
      ],
    },
    job: {
      J1: [
        () => (tm.set('T1', { next_action: 'merge' }), jobAt('needs_agent', { step: 'build', result: 'conflict in src/a.py' })),
        jobAt('needs_agent', { step: 'build', result: 'conflict in src/a.py' }),
        jobAt('running'),
        () => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded')),
      ],
    },
  })
  const agents = (prompt, opts) => (opts.label === 'merge-agent:T1' && tm.resume('J1'), 'resolved and resumed')

  const waitTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(waitTick.errors, [])
  assert.deepEqual(waitTick.work, [])
  assert.ok(waitTick.result.results[0].trail.includes('merge stopped for an agent; the next claim hands it over'))
  assert.deepEqual(releases(waitTick.ops), [])

  const handoverTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(handoverTick.errors, [])
  assert.equal(handoverTick.work.length, 1)
  assert.equal(handoverTick.work[0].opts.label, 'merge-agent:T1')
  assert.equal(handoverTick.work[0].opts.model, 'sonnet')
  for (const text of ['tm guide merge', 'Job: J1', 'stopped at build', 'Worktree: /est/.worktrees/land-T1', 'tm job status J1 prints what stopped it', 'tm job resume J1 --agent wf-s1-T1 --token k2', '--own-defect']) {
    assert.ok(handoverTick.work[0].prompt.includes(text), text)
  }
  assert.ok(
    !handoverTick.work[0].prompt.includes('conflict in src/a.py'),
    'the job result is not read for this prompt, and the merge agent reads it itself with tm job status',
  )
  assert.equal(handoverTick.result.results[0].status, 'COMPLETED')
  assert.deepEqual(releases(handoverTick.ops), [])
})

test('a landing the handed agent left stopped is released under its claim and counted', async () => {
  const tm = makeTm({
    chosen: [{ ...T1, action: 'merge' }],
    nodes: { T1: node('MERGING', 'merge') },
    start: { T1: [() => (tm.set('T1', { next_action: null }), claim('merge', { job: 'J1', token: 'k2' }))] },
    job: { J1: [jobAt('needs_agent', { step: 'push', result: 'push_failed' })] },
  })
  const { ops, result } = await runWave({ args: ARGS, tm })
  assert.deepEqual(releases(ops), [releaseOf('k2')])
  assert.ok(result.results[0].trail.includes('released, the merge agent left the job stopped'))
})

test('a landing that stops again after the agent resumed it is left parked, not released, for the next claim', async () => {
  const again = { step: 'gate', result: { reason: 'unattributed', resumed: 1 } }
  const tm = makeTm({
    chosen: [{ ...T1, action: 'merge' }],
    nodes: { T1: node('MERGING', 'merge') },
    parked: ['J1'],
    start: {
      T1: [
        () => (tm.set('T1', { next_action: null }), claim('merge', { job: 'J1', token: 'k2' })),
        () => (tm.set('T1', { next_action: null }), claim('merge', { job: 'J1', token: 'k3' })),
      ],
    },
    job: {
      J1: [
        jobAt('needs_agent', { step: 'build', result: { reason: 'conflict' } }),
        () => (tm.set('T1', { next_action: 'merge' }), jobAt('needs_agent', again)),
        jobAt('needs_agent', again),
        () => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded')),
      ],
    },
  })
  const agents = (prompt, opts) => (tm.resume('J1'), 'resumed')

  const parkedTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(parkedTick.errors, [])
  assert.deepEqual(releases(parkedTick.ops), [])
  assert.equal(starts(parkedTick.ops).length, 1)
  assert.ok(parkedTick.work[0].prompt.includes('--token k2'))

  const completeTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(completeTick.errors, [])
  assert.deepEqual(releases(completeTick.ops), [])
  assert.equal(starts(completeTick.ops).length, 1)
  assert.ok(completeTick.work[0].prompt.includes('--token k3'))
  assert.equal(completeTick.result.results[0].status, 'COMPLETED')
})

test("a container's landing follows the next repository's job to the end", async () => {
  const P1 = { id: 'P1', kind: 'plan', action: 'merge', model: 'sonnet', repos: ['core', 'web'], requires: [], job: null, migration: false }
  const tm = makeTm({
    chosen: [P1],
    nodes: { P1: { ...node('REVIEWED', 'merge', { outcome: 'approve' }), id: 'P1', kind: 'plan' } },
    start: { P1: [() => (tm.set('P1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1', repos: ['core', 'web'], branch: 'tm/P1' }))] },
    job: {
      J1: [jobAt('succeeded', { node_id: 'P1', result: { verify: 'ok', next: 'J2' } })],
      J2: [() => (tm.set('P1', { status: 'COMPLETED' }), jobAt('succeeded', { id: 'J2', node_id: 'P1', repo: 'web' }))],
    },
  })
  const { ops, result, errors } = await runWave({ args: ARGS, tm })
  assert.deepEqual(errors, [])
  assert.ok(ops.some(c => / job status J2 --wait 540 /.test(c)))
  assert.equal(result.results[0].status, 'COMPLETED')
})

test('a running job is polled with --wait, under the long runner timeout, until it leaves running', async () => {
  const tm = makeTm({
    chosen: [{ ...T1, action: 'merge' }],
    nodes: { T1: node('REVIEWED', 'merge', { outcome: 'approve' }) },
    start: { T1: [() => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' }))] },
    job: { J1: [jobAt('running'), jobAt('running'), () => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded'))] },
  })
  const { calls, work } = await runWave({ args: ARGS, tm })
  const waits = calls.filter(c => c.kind === 'op' && / job status J1 --wait 540 --fields \S+ 2>&1\)/.test(c.cmd))
  assert.equal(waits.length, 3)
  for (const w of waits) assert.ok(w.prompt.includes('600000'))
  assert.deepEqual(work, [])
})

test('a blocked claim naming a sync job waits it out, and a later run is what retries the claim', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: {
      T1: [
        json({ action: 'blocked', reason: 'syncing tm/P1', job: 'S1' }, 3),
        () => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1', base: 'tm/P1' })),
      ],
    },
    job: { S1: [jobAt('succeeded', { id: 'S1', kind: 'sync' })] },
  })
  const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')

  const waitTick = await runWave({ args: ARGS, tm, agents })
  const order = waitTick.ops.filter(c => / task start | job status /.test(c)).map(c => (/ job status /.test(c) ? 'job' : 'start'))
  assert.deepEqual(order, ['start', 'job'])
  assert.deepEqual(waitTick.work, [])

  const retryTick = await runWave({ args: ARGS, tm, agents })
  assert.equal(starts(retryTick.ops).length, 1)
  assert.deepEqual(retryTick.work.map(w => w.opts.label), ['implement:T1'])
})

test('a sync stopped for an agent is handed over by a later claim, on the family it names', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: {
      T1: [
        json({ action: 'blocked', reason: 'syncing tm/P1', job: 'S1' }, 3),
        claim('sync', { job: 'S1', base: 'tm/P1' }),
        () => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1', base: 'tm/P1' })),
      ],
    },
    job: {
      S1: [
        jobAt('needs_agent', { id: 'S1', kind: 'sync', target: 'tm/P1', step: 'build', result: 'conflict' }),
        jobAt('needs_agent', { id: 'S1', kind: 'sync', target: 'tm/P1', step: 'build', result: 'conflict' }),
        jobAt('running', { id: 'S1', kind: 'sync' }),
        jobAt('succeeded', { id: 'S1', kind: 'sync' }),
      ],
    },
  })
  const agents = (prompt, opts) => {
    if (opts.label === 'sync-agent:T1') tm.resume('S1')
    if (opts.label === 'implement:T1') tm.set('T1', { status: 'IMPLEMENTED', next_action: null })
    return 'done'
  }

  const waitTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(waitTick.work, [])

  const handoverTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(handoverTick.errors, [])
  assert.deepEqual(handoverTick.work.map(w => [w.opts.label, w.opts.model]), [['sync-agent:T1', 'sonnet']])
  assert.ok(handoverTick.work[0].prompt.includes('tm job resume S1 --agent wf-s1-T1 --token k1'))

  const implementTick = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(implementTick.work.map(w => w.opts.label), ['implement:T1'])

  const allOps = [...waitTick.ops, ...handoverTick.ops, ...implementTick.ops]
  assert.equal(starts(allOps).length, 3)
  assert.deepEqual(releases(allOps), [])
})

test('a sync the handed agent left stopped is released under its claim, so its lease does not hold the node', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    parked: ['S1'],
    start: { T1: [claim('sync', { job: 'S1', base: 'tm/P1', token: 'k2' })] },
    job: { S1: [jobAt('needs_agent', { id: 'S1', kind: 'sync', target: 'tm/P1', step: 'build', result: 'conflict' })] },
  })
  const { ops, result } = await runWave({ args: ARGS, tm, agents: () => 'could not resolve it' })
  assert.deepEqual(releases(ops), [releaseOf('k2')])
  assert.ok(result.results[0].trail.includes('released, the sync agent left the job stopped'))
})

for (const [base, from] of [['main', 'origin/main'], ['tm/S1', 'tm/S1']]) {
  test(`a container review based on ${base} reads every repository it touched from ${from}, on the container reviewer`, async () => {
    const P1 = { id: 'P1', kind: 'plan', action: 'review', model: 'opus', repos: ['core', 'web'], requires: [], job: null, migration: false }
    const tm = makeTm({
      chosen: [P1],
      nodes: { P1: { ...node('IMPLEMENTED', 'review'), id: 'P1', kind: 'plan' } },
      start: { P1: [() => (tm.set('P1', { status: 'REVIEWING', next_action: null }), claim('review', { model: 'opus', repos: ['core', 'web'], branch: 'tm/P1', base }))] },
    })
    const agents = () => (tm.set('P1', { status: 'REVIEWED', next_action: null }), 'done')
    const reviewerTypes = { task: 'task-reviewer', rereview: 'scoped-re-reviewer', container: 'branch-reviewer' }
    const { work } = await runWave({ args: { ...ARGS, reviewerTypes }, tm, agents })
    assert.equal(work[0].opts.agentType, 'branch-reviewer')
    assert.equal(work[0].opts.model, 'opus')
    assert.ok(work[0].prompt.includes(`git -C /est/core diff ${from}...tm/P1`))
    assert.ok(work[0].prompt.includes(`git -C /est/web diff ${from}...tm/P1`))
    assert.ok(work[0].prompt.includes('container review'))
    assert.ok(work[0].prompt.includes('/wt/core-P1-review') && work[0].prompt.includes('/wt/web-P1-review'))
  })
}

// T9 lands on main beside every container and is no node under it.
const PLAN_CHILDREN = { 'task list --plan P1': ['C1', 'C2'] }
const LANDED = [
  { name: 'its own landing merge', id: 'P1', kind: 'plan', lists: PLAN_CHILDREN, landings: ['T9', 'P1'], inner: ['C1'], files: ['C1.txt', 'P1.txt'] },
  { name: 'no landing merge, and children that landed on main', id: 'P1', kind: 'plan', lists: PLAN_CHILDREN, landings: ['C1', 'T9', 'C2'], files: ['C1.txt', 'C2.txt'] },
  { name: 'no landing merge, and nothing under it landed', id: 'P1', kind: 'plan', lists: PLAN_CHILDREN, landings: ['T9'], files: [] },
  {
    name: 'no landing merge, and a plan and a task under it that landed on main', id: 'S2', kind: 'spec',
    lists: { 'plan list --spec S2': ['PL1'], 'task list --spec S2': ['C1', 'C3'] }, landings: ['PL1', 'T9', 'C3'], files: ['C3.txt', 'PL1.txt'],
  },
]

for (const target of ['origin/main', 'tm/S1']) {
  for (const { name, id, kind, lists, landings, inner, files } of LANDED) {
    test(`a landed ${kind}'s review on ${target} with ${name} names only revisions there, and reads every landing under it`, async t => {
      const root = mkdtempSync(join(tmpdir(), 'wave-'))
      t.after(() => rmSync(root, { recursive: true, force: true }))
      const onto = target === 'origin/main' ? 'main' : target
      const repos = ['core', 'web']
      for (const repo of repos) landedRepo(join(root, repo), { id, landings, inner, onto })
      const tm = makeTm({
        chosen: [{ id, kind, action: 'review', model: 'opus', repos, requires: [], job: null, migration: false }],
        nodes: { [id]: { ...node('LANDED', 'review'), id, kind } },
        lists,
        start: { [id]: [() => (tm.set(id, { status: 'REVIEWING', next_action: null }), claim('review', { model: 'opus', repos, branch: target, base: onto }))] },
      })
      const agents = () => (tm.set(id, { status: 'COMPLETED', next_action: null }), 'done')
      const { work, errors } = await runWave({ args: { ...ARGS, root, reviewerTypes: REVIEWERS }, tm, agents })
      assert.deepEqual(errors, [])
      const scope = scopeOf(work[0])
      assert.equal(work[0].opts.agentType, 'branch-reviewer')
      assert.ok(scope.includes('container review'), scope)
      const commands = scope.match(new RegExp(`git -C \\S+ log [^']*'[^']*' ${target} --`, 'g')) || []
      assert.deepEqual(commands.map(c => c.split(' ')[2]), [`${root}/core`, `${root}/web`], scope)
      // git exits non-zero on a revision the repository does not hold, and execFileSync throws on it.
      for (const command of commands) {
        const patch = execFileSync('sh', ['-c', command], { encoding: 'utf8' })
        assert.deepEqual([...patch.matchAll(/^diff --git a\/(\S+)/gm)].map(m => m[1]).sort(), files, command)
      }
    })
  }
}

test('a landed container whose children cannot be read is released, never reviewed on a partial scope', async () => {
  const P1 = { id: 'P1', kind: 'plan', action: 'review', model: 'opus', repos: ['core'], requires: [], job: null, migration: false }
  const tm = makeTm({
    chosen: [P1],
    nodes: { P1: { ...node('LANDED', 'review'), id: 'P1', kind: 'plan' } },
    lists: PLAN_CHILDREN,
    start: { P1: [() => (tm.set('P1', { status: 'REVIEWING', next_action: null }), claim('review', { model: 'opus', branch: 'origin/main' }))] },
    corrupt: inner => / task list /.test(inner),
  })
  const { ops, work, result } = await runWave({ args: ARGS, tm })
  assert.deepEqual(work, [])
  assert.deepEqual(releases(ops), ['tm task release P1 --agent wf-s1-P1 --token k1 >/dev/null 2>&1'])
  assert.ok(result.results[0].trail.includes('released, the nodes under it could not be read'))
})

for (const [kind, id] of [['task', 'T1'], ['plan', 'P1']]) {
  test(`a sensitive ${kind}'s review after a fix is scoped to its open findings, on the re-reviewer`, async () => {
    const repos = kind === 'task' ? ['core'] : ['core', 'web']
    const tm = makeTm({
      chosen: [{ ...T1, id, kind, action: 'review', repos }],
      nodes: { [id]: node('FIXED', 'review', { id, kind, outcome: 'reject' }) },
      start: { [id]: [() => (tm.set(id, { status: 'REVIEWING', next_action: null }), claim('review', { repos, branch: `tm/${id}` }))] },
    })
    const agents = () => (tm.set(id, { status: 'REVIEWED', next_action: null }), 'done')
    const { work } = await runWave({ args: { ...ARGS, reviewerTypes: REVIEWERS }, tm, agents })
    assert.equal(work[0].opts.agentType, 'scoped-re-reviewer')
    assert.equal(
      scopeOf(work[0]),
      `Scope: every finding in tm section ${id}:review not yet recorded as closed, against the fix commits on tm/${id} and the fixer's latest :report entry, and, when the last landing failed, the failure its latest :merge entry names. Establish each closure by mutation.`,
    )
    assert.ok(!work[0].prompt.includes('container review'))
    assert.ok(work[0].prompt.includes(`tm task review ${id} --agent wf-s1-${id} --token k1 --approve or --reject`))
    assert.ok(work[0].prompt.includes('git worktree remove'))
    assert.ok(work[0].prompt.includes(`/wt/core-${id}-review`))
  })
}

test('a container fix across repositories names the worktree tm cut in each', async () => {
  const P1 = { id: 'P1', kind: 'plan', action: 'fix', model: 'opus', repos: ['core', 'web'], requires: [], job: null, migration: false }
  const worktrees = { core: '/wt/core-P1', web: '/wt/web-P1' }
  const tm = makeTm({
    chosen: [P1],
    nodes: { P1: { ...node('REVIEWED', 'fix', { outcome: 'reject' }), id: 'P1', kind: 'plan' } },
    start: { P1: [() => (tm.set('P1', { status: 'FIXING', next_action: null }), claim('fix', { model: 'opus', repos: ['core', 'web'], branch: 'tm/P1', worktree: '/wt/P1', worktrees }))] },
  })
  const agents = () => (tm.set('P1', { status: 'FIXED', next_action: null }), 'done')
  const { work } = await runWave({ args: ARGS, tm, agents })
  for (const text of ['core at /wt/core-P1', 'web at /wt/web-P1', 'branch tm/P1', 'tm task complete P1 --agent wf-s1-P1 --token k1']) {
    assert.ok(work[0].prompt.includes(text), text)
  }
  assert.ok(!work[0].prompt.includes('worktree add'))
})

for (const [outcome, pointer] of [['merge_failed', 'tm section get T1:merge'], ['reject', 'tm section get T1:review not recorded as closed']]) {
  test(`a fix answering ${outcome} is pointed at ${pointer.split(' ')[3]}`, async () => {
    const tm = makeTm({
      chosen: [{ ...T1, action: 'fix' }],
      nodes: { T1: node('REVIEWED', 'fix', { outcome }) },
      start: { T1: [() => (tm.set('T1', { status: 'FIXING', next_action: null }), claim('fix', { worktree: '/wt/core-T1' }))] },
    })
    const agents = () => (tm.set('T1', { status: 'FIXED', next_action: null }), 'done')
    const { work } = await runWave({ args: ARGS, tm, agents })
    assert.ok(work[0].prompt.includes(pointer), pointer)
    assert.ok(work[0].prompt.includes('tm guide fix'))
  })
}

for (const [capabilities, expected] of [[{}, undefined], [{ 'python-dev': ['figma'] }, 'python-dev']]) {
  test(`a node requiring figma goes to ${expected ?? 'the default agent'} when python-dev serves ${JSON.stringify(capabilities['python-dev'] ?? [])}`, async () => {
    const tm = makeTm({
      chosen: [{ ...T1, requires: ['figma'] }],
      nodes: { T1: node('READY', 'implement') },
      start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
    })
    const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')
    const { work } = await runWave({ args: { ...ARGS, agentTypes: { core: 'python-dev' }, capabilities }, tm, agents })
    assert.equal(work[0].opts.agentType, expected)
    assert.ok(work[0].prompt.includes('Requires: figma'))
  })
}

test('every op runs the tm the caller names', async () => {
  const tm = makeTm({ chosen: [T1], nodes: { T1: node('FAILED', null) } })
  const { ops } = await runWave({ args: { ...ARGS, tm: '/opt/tm-new/bin/tm' }, tm })
  assert.ok(ops.length >= 2)
  for (const cmd of ops) assert.ok(cmd.startsWith('/opt/tm-new/bin/tm ') || cmd.startsWith('out=$(/opt/tm-new/bin/tm '), cmd)
})

// op() names the estate by exporting TM_ROOT ahead of the command it runs, so the runner's own
// cwd never has to agree with args.root: an instruction naming "from <root>" could not be
// satisfied by a runner with a different cwd.
test('every op exports TM_ROOT for the estate instead of naming it in the instructions', async () => {
  const tm = makeTm({ chosen: [T1], nodes: { T1: node('FAILED', null) } })
  const { calls } = await runWave({ args: ARGS, tm })
  const ops = calls.filter(c => c.kind === 'op')
  assert.ok(ops.length >= 2)
  for (const c of ops) {
    assert.match(c.prompt, /\( export TM_ROOT=\/est; /)
    assert.ok(!c.prompt.includes('from /est,'), c.prompt)
  }
})

test('an op against the default root exports TM_ROOT=. rather than omitting it', async () => {
  const tm = makeTm({ chosen: [T1], nodes: { T1: node('FAILED', null) } })
  const { calls } = await runWave({ args: { session: 's1', worktreeDir: '/wt' }, tm })
  const ops = calls.filter(c => c.kind === 'op')
  assert.ok(ops.length >= 2)
  for (const c of ops) assert.match(c.prompt, /\( export TM_ROOT=\.; /)
})

test('read, start and job each keep their transcript small next to a job log the run never asked for', async () => {
  const tail = 'FAILED tests/test_x.py::test_case - see docs.pytest.org/en/stable/how-to/capture\n'.repeat(20)
  const tm = makeTm({
    chosen: [{ ...T1, action: 'merge' }],
    nodes: { T1: node('REVIEWED', 'merge', { outcome: 'approve', sections: tail }) },
    start: { T1: [() => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' }))] },
    job: {
      J1: [
        jobAt('running'),
        () => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded', { result: { tail, next: null, resumed: 0 } })),
      ],
    },
  })
  const { calls, errors } = await runWave({ args: ARGS, tm })
  assert.deepEqual(errors, [])
  const ops = calls.filter(c => c.kind === 'op')
  assert.ok(tail.length > 500)
  const byOp = kind => ops.filter(c => new RegExp(` ${kind} `).test(c.cmd))
  for (const [label, kind] of [['read', 'task get'], ['start', 'task start'], ['job', 'job status']]) {
    const matched = byOp(kind)
    assert.ok(matched.length > 0, `no ${label} op ran`)
    for (const c of matched) assert.ok(c.stdout.length < 500, `${label} transcript was ${c.stdout.length} bytes: ${c.stdout.slice(0, 80)}`)
  }
})
