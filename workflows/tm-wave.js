export const meta = {
  name: 'tm-wave',
  description: 'Choose dispatchable tm tasks on the given specs and pipeline each through implement, review, fix and merge, with every tm transition run by the script',
  whenToUse: 'Dispatcher tick. args: {specs, session, worktreeDir, slots, maxStrong, maxBatch, exclude, release, holdMerge, maxFixRounds, root, agentTypes, preamble, rulesDir, models}. root defaults to the session cwd; agentTypes (repo -> agent type) and preamble (repo -> brief preamble line, plus a "default" key) default to none; rulesDir (a path to read every rule file from before the first edit) defaults to none; models (family -> model id) defaults to the current Claude ids.',
  phases: [
    { title: 'Discover', detail: '`tm wave discover` chooses the batch', model: 'haiku' },
    { title: 'Claim', detail: 'tm run start, and the lease worktree it made', model: 'haiku' },
    { title: 'Implement' },
    { title: 'Review-hash', detail: ':review section hash before and after each review', model: 'haiku' },
    { title: 'Review' },
    { title: 'Fix' },
    { title: 'Merge' },
    { title: 'Verify', detail: 'merged sha on origin/main, then tm verify run', model: 'haiku' },
    { title: 'Release', detail: 'tm run stop to the next status', model: 'haiku' },
    { title: 'Hold', detail: ':hold section on a cap or an unmet blocker', model: 'haiku' },
  ],
}

const A = args || {}
const SPECS = A.specs || []
const SESSION = A.session
const WT = A.worktreeDir
if (!SPECS.length || !SESSION || !WT) throw new Error('args.specs, args.session and args.worktreeDir are required')
const SLOTS = A.slots || 9
const MAX_STRONG = A.maxStrong || 5
const MAX_FIX = A.maxFixRounds || 2
const HOLD_MERGE = new Set(A.holdMerge || [])

// The tm root a bare command runs against; a caller with no fixed estate path leaves this out
// and every op runs from wherever the dispatching session already sits.
const ROOT = A.root || '.'
const MODEL_ID = A.models || { haiku: 'claude-haiku-4-5', sonnet: 'claude-sonnet-5', opus: 'claude-opus-5', fable: 'claude-fable-5-1' }
// repo -> agent type for the implement/fix dispatch; a repo missing here gets the harness default.
const AGENT_TYPE = A.agentTypes || {}
// repo -> a line prepended to every brief for that repo, plus an optional "default" for every
// other repo; leave both out for no preamble at all.
const PREAMBLE = A.preamble || {}
// A path read for every rule file before the first edit or probe; leave out where the project
// carries no such directory.
const RULES_DIR = A.rulesDir || null

const SAFE = /^[A-Za-z0-9._\/-]+$/
const q = s => {
  if (!SAFE.test(String(s))) throw new Error(`refusing to interpolate ${JSON.stringify(s)}`)
  return s
}

const PHASE = { discover: 'Discover', claim: 'Claim', 'review-hash': 'Review-hash', verify: 'Verify', release: 'Release', hold: 'Hold' }

// A session started before .claude/agents/tm-op.md existed does not know the type, so the first
// failure hands this and every later command to the generic runner.
let opType = 'tm-op'
async function runner(prompt, opts) {
  if (opType) {
    const r = await agent(prompt, { ...opts, agentType: opType }).catch(() => null)
    if (r) return r
    if (opType) log(`${opts.label}: the tm-op runner returned nothing; the generic runner takes the rest of this run`)
    opType = undefined
  }
  return agent(prompt, opts)
}

// The script has no shell: a runner agent executes a command the script composed, and the
// __EXIT sentinel is parsed from its stdout rather than trusting the runner's own account.
// Each kind's command controls its whole stdout, so `shape` (last group: the exit code) doubles as
// the schema pattern that rejects a transcription of the wrong shape before the script sees it.
async function op(kind, id, cmd, shape) {
  const schema = { type: 'object', properties: { stdout: { type: 'string', pattern: shape.source } }, required: ['stdout'] }
  for (let attempt = 0; attempt < 2; attempt++) {
    const r = await runner(`tm-task: none — command runner for the tm-wave workflow
Model: claude-haiku-4-5
Run this exact command once with the Bash tool, from ${ROOT}, changing nothing in it, and run no other command:

( ${cmd} ); echo "__EXIT:$?"

Return its complete stdout, character for character, in the stdout field: every line, unparsed and unreformatted, even where a line is JSON.`,
      { label: `${kind}:${id}`, phase: PHASE[kind], model: 'haiku', effort: 'low', schema })
    const m = r && typeof r.stdout === 'string' && r.stdout.match(shape)
    if (m) return { exit: Number(m[m.length - 1]), m }
  }
  return null
}

// Matches djb2 in taskmanager.engine.wave; its payload is ASCII because json.dumps escapes the rest.
function djb2(s) {
  let h = 5381
  for (let i = 0; i < s.length; i++) h = (Math.imul(h, 33) + s.charCodeAt(i)) >>> 0
  return h
}

// Returns the batch, or null when the runner's transcription fails its exit code or checksum.
async function discover(attempt) {
  const cmd = [
    'tm wave discover',
    ...SPECS.map(s => `--spec ${q(s)}`),
    `--session ${q(SESSION)}`,
    `--slots ${SLOTS | 0}`,
    `--max-strong ${MAX_STRONG | 0}`,
    ...(A.exclude || []).map(x => `--exclude ${q(x)}`),
    ...(A.release || []).map(x => `--release ${q(x)}`),
  ].join(' ')
  const r = await op('discover', `attempt-${attempt}`, cmd, /^([\s\S]*?)__EXIT:(\d+)\s*$/)
  if (!r || r.exit !== 0) return null
  const lines = r.m[1].split('\n').map(l => l.trim()).filter(Boolean)
  const check = (lines.pop() || '').match(/^__CHECK n=(\d+) h=(\d+)$/)
  const payload = lines.pop() || ''
  try {
    const d = JSON.parse(payload)
    return check && djb2(payload) === Number(check[2]) && d.chosen.length === Number(check[1]) ? d : null
  } catch (e) {
    return null
  }
}

phase('Discover')
let plan = null
for (let i = 1; i <= 3 && !plan; i++) {
  plan = await discover(i)
  if (!plan) log(`discovery attempt ${i} failed its exit code or checksum`)
}
if (!plan) throw new Error('discovery failed three times; nothing was claimed')
// A run holds at most min(16, CPUs - 2) agents at once, so a batch past that sits queued and unclaimed.
if (A.maxBatch && plan.chosen.length > A.maxBatch) {
  log(`maxBatch ${A.maxBatch}: left for the next run: ${plan.chosen.slice(A.maxBatch).map(t => t.id).join(', ')}`)
  plan.chosen = plan.chosen.slice(0, A.maxBatch)
}
plan.held.forEach(h => log(`held: ${h}`))
log(`wave: ${plan.chosen.map(t => `${t.id}@${t.status}/${t.model}`).join(', ') || 'nothing dispatchable'}; ${plan.waiting_for_slot} waiting for a slot`)

// True once tm printed the requested status. Anything else leaves the lease where it was, which
// blocks the task's declared files for every sibling, so it is logged for the dispatcher.
async function release(t, status) {
  const r = await op('release', t.id,
    `tm run stop ${q(t.id)} --status ${q(status)} 2>/dev/null | grep -o 'with status [A-Z_]*'`,
    /^(?:with status ([A-Z_]+)\n)?__EXIT:(\d+)\s*$/)
  const printed = r && r.exit === 0 ? r.m[1] : null
  if (printed !== status) log(`release:${t.id}: tm run stop --status ${status} printed ${printed || 'nothing'}; its lease may still be held`)
  return printed === status
}

// Returns { wt } (the lease worktree path, or '-' without --worktree) or { refused } with tm's reason.
// A start that took but whose worktree could not be read is released back to `back`.
async function claim(t, role, model, { ttl, worktree, back }) {
  const id = q(t.id)
  const wt = worktree ? ` --worktree --worktree-dir ${q(WT)}` : ''
  const lookup = `python3 -c "import json,sys; print('worktree: ' + next((l.get('worktree_path') or '-') for l in json.load(sys.stdin)['leases'] if l['task_id']=='${id}'))"`
  const r = await op('claim', t.id,
    `out=$(tm run start ${id} --agent wf-${q(role)}-${q(model)}-${id} --session ${q(SESSION)} --ttl ${ttl | 0}${wt} 2>&1) || { printf 'refused: %s\\n' "$(printf '%s' "$out" | tr -s '\\n' ' ' | tr -cd '[:print:]' | cut -c1-200)"; exit 1; }; tm run list --json | ${lookup}`,
    /^(?:(worktree|refused): ([^\n]*)\n)?__EXIT:(\d+)\s*$/)
  if (!r) return { refused: 'the runner returned no parseable output; tm run list shows whether the lease was taken' }
  if (r.m[1] === 'refused') return { refused: r.m[2] || 'tm run start exited non-zero' }
  if (r.m[1] === 'worktree' && r.exit === 0) return { wt: r.m[2] }
  await release(t, back)
  return { refused: `started, but its lease worktree could not be read; released to ${back}` }
}

// The hold section is what keeps discovery from choosing the task again, so a failed write is logged.
async function hold(t, reason) {
  const text = `${reason.replace(/'/g, '’')} (tm-wave, session ${SESSION}). Release with args.release once resolved.`
  const file = `${WT}/hold-${t.id}.md`
  const r = await op('hold', t.id,
    `printf '%s\\n' '${text}' > ${q(file)} && tm section set ${q(t.id)}:hold --file ${q(file)} --header "## Hold" >/dev/null 2>&1`,
    /^__EXIT:(\d+)\s*$/)
  if (!r || r.exit !== 0) log(`hold:${t.id}: the :hold section was not written; the next discovery may choose this task again`)
  return !!r && r.exit === 0
}

// Twelve hex chars of the :review section's hash (an absent section hashes as empty), or null.
async function reviewHash(t) {
  const r = await op('review-hash', t.id,
    `tm section get ${q(t.id)}:review 2>/dev/null | shasum | cut -c1-12`,
    /^([0-9a-f]{12})\n__EXIT:(\d+)\s*$/)
  return r && r.exit === 0 ? r.m[1] : null
}

// 'ok', or which step failed: fetch-failed, not-on-origin-main, verify-failed, runner-failed.
async function verifyMerged(t, repo, sha) {
  const g = `git -C ${q(repo)}`
  const r = await op('verify', t.id,
    `${g} fetch -q origin main || { echo fetch-failed; exit 1; }; ${g} merge-base --is-ancestor ${q(sha)} origin/main || { echo not-on-origin-main; exit 1; }; tm verify run ${q(t.id)} >/dev/null 2>&1 || { echo verify-failed; exit 1; }; echo ok`,
    /^(ok|fetch-failed|not-on-origin-main|verify-failed)\n__EXIT:(\d+)\s*$/)
  if (!r) return 'runner-failed'
  return r.m[1] === 'ok' && r.exit !== 0 ? 'verify-failed' : r.m[1]
}

const head = (t, role, model) => {
  const preamble = PREAMBLE[t.repo] ?? PREAMBLE.default ?? ''
  const rules = RULES_DIR
    ? `\nRules: read every file in ${RULES_DIR} yourself before your first edit or probe; path-scoped rules do not load in a worktree.`
    : ''
  return `${preamble ? preamble + '\n' : ''}tm-task: ${t.id}
Model: ${MODEL_ID[model]}
The tm-wave workflow that dispatched you holds this task's lease and moves its status. Do not run tm run start, stop, release, heartbeat or sweep, and do not claim or release any task. Read tm guide ${role} and follow it, skipping only its claim and release steps.
Brief: tm render ${t.id} --view subagent${rules}
Sections: before any tm section set, tm section get the same key and append to it. Code, comments, test names, log lines and fixtures never name a ruling, task, review or round.`
}

const WORK = {
  type: 'object',
  properties: { outcome: { type: 'string', enum: ['done', 'blocked'] }, summary: { type: 'string' } },
  required: ['outcome', 'summary'],
}
const REVIEWED = {
  type: 'object',
  properties: { open: { type: 'integer', description: 'findings still open after this review' }, summary: { type: 'string' } },
  required: ['open', 'summary'],
}
const MERGED = {
  type: 'object',
  properties: {
    outcome: { type: 'string', enum: ['merged', 'needs_fixes', 'blocked'] },
    sha: { type: 'string', description: 'the commit now on origin/main, when merged' },
    summary: { type: 'string' },
  },
  required: ['outcome', 'summary'],
}

async function implement(t, trail) {
  const c = await claim(t, 'impl', t.model, { ttl: 10800, worktree: true, back: 'NOT_STARTED' })
  if (!c.wt) return trail.push(`implement: claim refused — ${c.refused}`), null
  const r = await agent(`${head(t, 'implement', t.model)}
Worktree: ${c.wt} — branch tm/${t.id}, cut from ${t.repo} origin/main. Work only there, and never cd in a Bash command.
Report: append to tm section ${t.id}:report before your last commit.
Return done once the work is committed on the branch and the report is written, or blocked with the reason.`,
    { label: `impl:${t.id}`, phase: 'Implement', model: t.model, agentType: AGENT_TYPE[t.repo], schema: WORK })
  if (!r) return await release(t, 'NOT_STARTED'), trail.push('implement: agent died, returned to NOT_STARTED'), null
  trail.push(`implement: ${r.outcome} — ${r.summary}`)
  if (r.outcome !== 'done') return await release(t, 'NOT_STARTED'), null
  await release(t, 'WAITING_REVIEW')
  return 'WAITING_REVIEW'
}

async function review(t, round, trail) {
  const before = await reviewHash(t)
  if (!before) return trail.push(`review ${round}: the :review section hash could not be read, so the review could not be checked; not claimed`), null
  const model = round > 0 ? 'sonnet' : t.review
  // The defined reviewer agents carry no MCP tools, and a Figma deliverable can only be read through one.
  const needsFigma = t.blockers.some(b => /figma/i.test(b))
  const reviewer = needsFigma ? undefined : round > 0 ? 'scoped-re-reviewer' : 'task-reviewer'
  const c = await claim(t, 'review', model, { ttl: 3600, worktree: false, back: 'WAITING_REVIEW' })
  if (!c.wt) return trail.push(`review ${round}: claim refused — ${c.refused}`), null
  const r = await agent(`${head(t, 'review', model)}
Scope: ${round === 0
    ? `the whole diff of tm/${t.id} from git merge-base origin/main tm/${t.id}, against the brief and the spec.`
    : `every finding in the :review section not yet recorded as closed, against the fix commits on tm/${t.id} and the fixer's latest :report entry. Establish each closure by mutation.`}
${needsFigma ? 'Read the Figma file through the figma MCP (load it with ToolSearch); you are a reviewer, so change nothing in Figma or in any file.\n' : ''}Findings: append numbered findings to tm section ${t.id}:review. A finding is open or closed; there is no non-blocking class. Write the section even when nothing is open, saying so.
Return the number of findings still open.`,
    { label: `review${round}:${t.id}`, phase: 'Review', model, agentType: reviewer, schema: REVIEWED })
  if (!r) return await release(t, 'WAITING_REVIEW'), trail.push(`review ${round}: agent died`), null
  const after = await reviewHash(t)
  if (after === null || after === before) {
    const what = after === null ? 'its :review section hash could not be read afterwards' : 'it left the :review section unchanged'
    await release(t, 'WAITING_REVIEW')
    await hold(t, `review ${round} returned ${r.open} open findings but ${what}`)
    return trail.push(`review ${round}: ${what} — held`), null
  }
  trail.push(`review ${round}: ${r.open} open — ${r.summary}`)
  const next = r.open > 0 ? 'WAITING_FIXES' : 'WAITING_MERGE'
  await release(t, next)
  return next
}

async function fix(t, round, trail) {
  const model = t.migration && t.model !== 'sonnet' ? t.model : 'sonnet'
  const c = await claim(t, 'fix', model, { ttl: 7200, worktree: true, back: 'WAITING_FIXES' })
  if (!c.wt) return trail.push(`fix ${round}: claim refused — ${c.refused}`), null
  const r = await agent(`${head(t, 'fix', model)}
Worktree: ${c.wt} — branch tm/${t.id}. Work only there, and never cd in a Bash command.
Findings: every finding in tm section get ${t.id}:review not recorded as closed. Fix each one, commit on the branch, and answer each by number in an appended :report entry.
Return done, or blocked with the reason.`,
    { label: `fix${round}:${t.id}`, phase: 'Fix', model, agentType: AGENT_TYPE[t.repo], schema: WORK })
  if (!r) return await release(t, 'WAITING_FIXES'), trail.push(`fix ${round}: agent died`), null
  trail.push(`fix ${round}: ${r.outcome} — ${r.summary}`)
  if (r.outcome !== 'done') {
    await release(t, 'WAITING_FIXES')
    await hold(t, `fix ${round} blocked: ${r.summary}`)
    return null
  }
  await release(t, 'WAITING_REVIEW')
  return 'WAITING_REVIEW'
}

async function merge(t, trail) {
  const c = await claim(t, 'merge', 'sonnet', { ttl: 3600, worktree: false, back: 'WAITING_MERGE' })
  if (!c.wt) return trail.push(`merge: claim refused — ${c.refused}`), null
  const repo = `${ROOT}/${t.repo}`
  const mwt = `${WT}/${t.repo}-merge-${t.id}`
  const r = await agent(`${head(t, 'merge', 'sonnet')}
Merge worktree: after git -C ${repo} fetch -q origin, git -C ${repo} worktree add --detach ${mwt} origin/main. Remove it by that path with git worktree remove when you are done.
Merge tm/${t.id} there (a --no-ff merge with a quoted subject when origin/main has moved past the branch's base), run the task's own gate, re-measure git ls-remote origin main in the same breath as the push, and push only as git push origin HEAD:main. Never force; never retry a refused push.
${t.migration ? 'Migration: re-derive the migration head from origin/main immediately before the push, and renumber if the revision is taken.\n' : ''}${t.blockers.length ? `External blockers, each checked before merging: ${JSON.stringify(t.blockers)}. If one is unmet, do not merge; return blocked naming it.\n` : ''}If any :review finding is still open, do not merge; return needs_fixes.
Outcome: append to tm section ${t.id}:merge.
Return merged with the commit sha now on origin/main, needs_fixes, or blocked with the reason.`,
    { label: `merge:${t.id}`, phase: 'Merge', model: 'sonnet', schema: MERGED })
  if (!r) return await release(t, 'WAITING_MERGE'), trail.push('merge: agent died'), null
  trail.push(`merge: ${r.outcome} ${r.sha || ''} — ${r.summary}`)
  if (r.outcome === 'needs_fixes') return await release(t, 'WAITING_FIXES'), 'WAITING_FIXES'
  if (r.outcome === 'merged' && /^[0-9a-f]{7,40}$/.test(r.sha || '')) {
    const v = await verifyMerged(t, repo, r.sha)
    if (v === 'ok') return await release(t, 'COMPLETED'), 'COMPLETED'
    await release(t, 'WAITING_MERGE')
    await hold(t, `merge reported ${r.sha} but its verification failed: ${v}`)
    return trail.push(`merge: verification ${v} — held`), null
  }
  await release(t, 'WAITING_MERGE')
  await hold(t, `merge ${r.outcome}: ${r.summary}`)
  return null
}

async function run(t) {
  const trail = []
  let status = t.status, round = 0, fixes = 0
  if (status === 'READY') status = await implement(t, trail)
  while (status === 'WAITING_REVIEW' || status === 'WAITING_FIXES' || status === 'WAITING_MERGE') {
    if (status === 'WAITING_REVIEW') status = await review(t, round, trail)
    else if (status === 'WAITING_FIXES') {
      if (fixes >= MAX_FIX) {
        await hold(t, `${MAX_FIX} fix rounds spent with findings still open: correct the brief or escalate the model`)
        trail.push('fix cap reached — held at WAITING_FIXES')
        break
      }
      fixes++; round++
      status = await fix(t, round, trail)
    } else {
      if (HOLD_MERGE.has(t.id)) { trail.push('merge held by args.holdMerge'); break }
      status = await merge(t, trail)
    }
  }
  return { id: t.id, status: status || 'stopped', trail }
}

const results = await pipeline(plan.chosen, t => run(t))
return { results: results.filter(Boolean), held: plan.held, waitingForSlot: plan.waiting_for_slot }
