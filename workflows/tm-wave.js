export const meta = {
  name: 'tm-wave',
  description: 'Choose claimable tm nodes, claim each one\'s next step, run it, and stop: tm task start names the step and its model, an agent does it and closes it, a landing runs as a tm job, and the next tick claims the next step',
  whenToUse: 'Dispatcher tick. args: {session, worktreeDir, specs, slots, maxStrong, maxBatch, exclude, holdMerge, root, tm, agentTypes, reviewerTypes, capabilities, preamble, rulesDir, gateLane, models}. session and worktreeDir are required; specs defaults to every spec; root defaults to the session cwd and tm to the tm on PATH; agentTypes (repo -> agent type for implement and fix), reviewerTypes ({task, rereview, container} -> agent type), capabilities (agent type -> the requires values it serves), preamble (repo -> a line prepended to its briefs, plus a "default" key) and rulesDir default to none; gateLane (a string, or repo -> text with a "default" key; `{task}` becomes the node id) defaults to none; models (the family tm names on a claim -> model id) overrides the current Claude ids family by family.',
  phases: [
    { title: 'Discover', detail: '`tm wave discover` chooses the batch', model: 'haiku' },
    { title: 'Claim', detail: 'tm task get and tm task start, and a release when a step is left open', model: 'haiku' },
    { title: 'Implement' },
    { title: 'Review' },
    { title: 'Fix' },
    { title: 'Land', detail: 'tm job status until a landing or sync leaves running', model: 'haiku' },
    { title: 'Land agent', detail: 'a landing or sync tm stopped for an agent' },
  ],
}

const A = args || {}
const SESSION = A.session
const WT = A.worktreeDir
if (!SESSION || !WT) throw new Error('args.session and args.worktreeDir are required')
const RETIRED = {
  release: 'a node waits only on what tm task release --blocked names, and the workflow holds nothing',
  maxFixRounds: 'tm counts fix rounds itself; set max_fix_rounds with tm config set',
}
for (const key of Object.keys(RETIRED)) {
  if (key in A) throw new Error(`args.${key} is no longer read: ${RETIRED[key]}`)
}
const SPECS = A.specs || []
const SLOTS = A.slots || 9
const MAX_STRONG = A.maxStrong || 5
const HOLD_MERGE = new Set(A.holdMerge || [])

// The tm root a bare command runs against; a caller with no fixed estate path leaves this out
// and every op runs from wherever the dispatching session already sits.
const ROOT = A.root || '.'
// tm names a family on every claim; this maps it to the id a brief's Model: line carries.
const MODEL_ID = { haiku: 'claude-haiku-4-5', sonnet: 'claude-sonnet-5', opus: 'claude-opus-5', fable: 'claude-fable-5-1', ...A.models }
// repo -> agent type for implement and fix; a repo missing here gets the harness default.
const AGENT_TYPE = A.agentTypes || {}
// {task, rereview, container} -> agent type for a first task review, a review after a fix, and a
// plan's or spec's review.
const REVIEWER = A.reviewerTypes || {}
// agent type -> the `requires` values it can serve; a type not listed serves none.
const CAPS = A.capabilities || {}
const PREAMBLE = A.preamble || {}
const RULES_DIR = A.rulesDir || null
// Many concurrent agents running suites on one machine starve each other, so a project with a
// remote runner names it here and every implement, review and fix brief carries it.
const GATE_LANE = typeof A.gateLane === 'string' ? { default: A.gateLane } : A.gateLane || {}

const SAFE = /^[A-Za-z0-9._\/-]+$/
const q = s => {
  if (!SAFE.test(String(s))) throw new Error(`refusing to interpolate ${JSON.stringify(s)}`)
  return s
}
const TM = q(A.tm || 'tm')

// tm job status --wait returns inside the runner's 10-minute command limit; a job still running
// after MAX_POLLS waits keeps running on its own and a later tick picks it up.
const WAIT_SECONDS = 540
const MAX_POLLS = 8
const TERMINAL = new Set(['COMPLETED', 'FAILED', 'DEFERRED', 'ABANDONED', 'SUPERSEDED'])
const CLAIMED = { implement: 'IMPLEMENTING', review: 'REVIEWING', fix: 'FIXING' }
const ACTIONS = ['implement', 'review', 'fix', 'merge', 'sync']
const PHASE = { discover: 'Discover', read: 'Claim', start: 'Claim', release: 'Claim', job: 'Land' }
const WORK_PHASE = { implement: 'Implement', review: 'Review', fix: 'Fix' }

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
// The command exports TM_ROOT ahead of itself so every workflow tm call names its estate whatever
// the runner's own cwd happens to be, rather than depending on the runner starting somewhere in
// particular. `shape` (last group: the exit code) doubles as the schema pattern that rejects a
// transcription of the wrong shape before the script sees it; `valid` rejects one of the right
// shape but wrong bytes, and either rejection is retried while attempts remain.
async function op(kind, id, cmd, shape, { attempts = 2, long = false, valid = () => true } = {}) {
  const schema = { type: 'object', properties: { stdout: { type: 'string', pattern: shape.source } }, required: ['stdout'] }
  const timeout = long ? ' Set the Bash tool timeout to 600000 ms: the command waits up to nine minutes.' : ''
  for (let attempt = 0; attempt < attempts; attempt++) {
    const r = await runner(`tm-task: none — command runner for the tm-wave workflow
Model: ${MODEL_ID.haiku}
Run this exact command once with the Bash tool, changing nothing in it, and run no other command.${timeout}

( export TM_ROOT=${q(ROOT)}; ${cmd} ); echo "__EXIT:$?"

Return its complete stdout, character for character, in the stdout field: every line, unparsed and unreformatted, even where a line is JSON.`,
      { label: `${kind}:${id}`, phase: PHASE[kind], model: 'haiku', effort: 'low', schema })
    const m = r && typeof r.stdout === 'string' && r.stdout.match(shape)
    if (m && valid(m)) return { exit: Number(m[m.length - 1]), m }
  }
  return null
}

// Over UTF-8 bytes, as tm and the shell compute it, so text carrying non-ASCII still checks.
function djb2(s) {
  const bytes = unescape(encodeURIComponent(s))
  let h = 5381
  for (let i = 0; i < bytes.length; i++) h = (Math.imul(h, 33) + bytes.charCodeAt(i)) >>> 0
  return h
}

// POSIX cksum (CRC-32 over the bytes, then the length), exactly as `cksum` prints it. The runner
// pipes through the stock tool rather than a hash written into the command, because a model runner
// has rewritten such code before executing it, and every claim it touched was released.
function cksum(s) {
  const bytes = unescape(encodeURIComponent(s))
  let crc = 0
  const step = b => {
    crc = (crc ^ (b << 24)) >>> 0
    for (let i = 0; i < 8; i++) crc = crc & 0x80000000 ? ((crc << 1) ^ 0x04C11DB7) >>> 0 : (crc << 1) >>> 0
  }
  for (let i = 0; i < bytes.length; i++) step(bytes.charCodeAt(i))
  for (let n = bytes.length; n > 0; n = Math.floor(n / 256)) step(n & 0xff)
  return `${(~crc) >>> 0} ${bytes.length}`
}

// A tm command whose stdout is JSON, checked against a cksum the same shell computed over the same
// bytes, so a paraphrased field never reaches a decision.
async function opJson(kind, id, cmd, opts = {}) {
  const r = await op(kind, id,
    `out=$(${cmd} 2>&1); rc=$?; printf '%s\\n' "$out"; printf '__CHECK '; printf '%s' "$out" | cksum; exit $rc`,
    /^([\s\S]*)\n__CHECK (\d+ \d+)\n__EXIT:(\d+)\s*$/,
    { ...opts, valid: m => cksum(m[1]) === m[2] })
  if (!r) return null
  let data = null
  try {
    data = JSON.parse(r.m[1])
  } catch (e) {
    data = null
  }
  return { exit: r.exit, data, text: r.m[1] }
}

// Returns the batch, or null when the runner's transcription fails its exit code or checksum.
async function discover(attempt) {
  const cmd = [
    `${TM} wave discover`,
    ...SPECS.map(s => `--spec ${q(s)}`),
    `--session ${q(SESSION)}`,
    `--slots ${SLOTS | 0}`,
    `--max-strong ${MAX_STRONG | 0}`,
    ...(A.exclude || []).map(x => `--exclude ${q(x)}`),
    ...[...HOLD_MERGE].map(x => `--hold-merge ${q(x)}`),
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

const agentName = n => `wf-${q(SESSION)}-${q(n.id)}`
// Every tick of a session claims a node under the same agent name, so the claim's token is what
// tells this claim's step apart from a later claim of the same node.
const owner = (n, token) => `--agent ${agentName(n)}${token ? ` --token ${q(token)}` : ''}`
const clip = v => String((typeof v === 'string' ? v : JSON.stringify(v)) ?? '').replace(/\s+/g, ' ').slice(0, 300)
// The harness's default agent reaches every connected tool, so a node needing a capability the
// preferred type is not known to serve goes to the default instead.
const pickType = (type, requires = []) => (requires.every(x => (CAPS[type] || []).includes(x)) ? type : undefined)

// Only these three: run() and work() read status, next_action and outcome off what this
// returns, and nothing else -- a node's sections and job logs are the bulk of the row.
const READ_FIELDS = 'status,next_action,outcome'

async function read(n) {
  const r = await opJson('read', n.id, `${TM} task get ${q(n.id)} --json --fields ${READ_FIELDS}`)
  return r && r.exit === 0 && r.data && typeof r.data.status === 'string' ? r.data : null
}

// Only this claim's own lease is released: tm refuses --agent and --token for any other, so a
// release racing a later claim of the node changes nothing.
async function release(n, trail, why, token) {
  const r = await op('release', n.id, `${TM} task release ${q(n.id)} ${owner(n, token)} >/dev/null 2>&1`, /^__EXIT:(\d+)\s*$/)
  const refused = r && r.exit === 0 ? '' : ' (tm refused the release or the runner failed; tm run sweep returns the step once its lease expires)'
  trail.push(`released, ${why}${refused}`)
}

// A claim is a write, so it is never retried: a second attempt would find the first one's lease.
async function start(n, trail) {
  const r = await opJson('start', n.id,
    `${TM} task start ${q(n.id)} --agent ${agentName(n)} --session ${q(SESSION)} --worktree-dir ${q(WT)} --json`,
    { attempts: 1 })
  if (!r) {
    await release(n, trail, 'its claim could not be read')
    return null
  }
  const d = r.data || {}
  if (r.exit === 3 || d.action === 'blocked') return { ...d, action: 'blocked', reason: d.reason || 'tm task start exited 3' }
  if (r.exit !== 0) {
    trail.push(`claim refused: ${clip(r.text)}`)
    return null
  }
  if (!ACTIONS.includes(d.action)) {
    await release(n, trail, `its claim printed no step: ${clip(r.text)}`)
    return null
  }
  if (!Object.hasOwn(MODEL_ID, d.model)) {
    trail.push(`claim names the model family ${clip(d.model)}, which args.models does not map`)
    await release(n, trail, 'no model id was known for it', d.token)
    return null
  }
  return d
}

// Only these: land() reads state, kind, repo, target, step and worktree off what this returns,
// and from result only next and resumed -- a stopped job's own summary is the bulk of its row, and
// the merge agent reads that itself with `tm job status`.
const JOB_FIELDS = 'state,kind,repo,target,step,worktree,result.next,result.resumed'

async function job(n, id, wait) {
  const r = await opJson('job', n.id,
    `${TM} job status ${q(id)}${wait ? ` --wait ${WAIT_SECONDS}` : ''} --fields ${JOB_FIELDS}`, { long: wait })
  return r && r.exit === 0 && r.data && typeof r.data.state === 'string' ? r.data : null
}

// --agent and --token name the lease this claim took, so tm refuses the close once it is gone.
const close = (n, c) => ({
  implement: `${TM} task complete ${n.id} ${owner(n, c.token)}`,
  fix: `${TM} task complete ${n.id} ${owner(n, c.token)}`,
  review: `${TM} task review ${n.id} ${owner(n, c.token)} --approve or --reject (tm refuses either until your findings are in its :review section)`,
  merge: `${TM} job resume ${c.job} ${owner(n, c.token)}`,
  sync: `${TM} job resume ${c.job} ${owner(n, c.token)}`,
})[c.action]

const head = (n, c, fam, role) => {
  const repo = (c.repos || n.repos || [])[0]
  const preamble = PREAMBLE[repo] ?? PREAMBLE.default ?? ''
  const rules = RULES_DIR
    ? `\nRules: read every file in ${RULES_DIR} yourself before your first edit or probe; path-scoped rules do not load in a worktree.`
    : ''
  // `{task}` in a lane becomes this node's id, so a runner that tags its jobs can name them after it.
  const lane = (GATE_LANE[repo] ?? GATE_LANE.default ?? '').replaceAll('{task}', n.id)
  const gate = lane ? `\nGate lane: ${lane}` : ''
  const requires = n.requires || []
  const needs = requires.length ? `\nRequires: ${requires.join(', ')}; load the tools that provide it with ToolSearch before the first step that needs them.` : ''
  const blocked = c.action in CLAIMED
    ? `, or, when something outside this step must happen first, with ${TM} task release ${n.id} ${owner(n, c.token)} --blocked naming the edge, decision or condition it waits on`
    : ''
  return `${preamble ? preamble + '\n' : ''}tm-task: ${n.id}
Model: ${MODEL_ID[fam]}
The tm-wave workflow claimed this ${c.action} step for you: never run tm task start, and never claim or release any other node. Read tm guide ${role} and follow it from the step after its claim. Close the step with ${close(n, c)}${blocked}.
Brief: tm render ${n.id} --view subagent${rules}${gate}${needs}
Sections: before any tm section set, tm section get the same key and append to it. Code, comments, test names, log lines and fixtures never name a ruling, task, review or round.`
}

async function work(n, c, s, trail) {
  const fam = c.model
  const repos = c.repos || []
  let type = AGENT_TYPE[repos[0]]
  let body
  if (c.action === 'review') {
    const again = s.status === 'FIXED'
    const container = n.kind !== 'task'
    type = REVIEWER[container ? 'container' : again ? 'rereview' : 'task']
    const base = !c.base || c.base === 'main' ? 'origin/main' : c.base
    body = again
      ? `Scope: every finding in tm section ${n.id}:review not yet recorded as closed, against the fix commits on ${c.branch} and the fixer's latest :report entry, and, when the last landing failed, the failure its latest :merge entry names. Establish each closure by mutation.`
      : `Scope: the whole diff of ${c.branch} from its base, in each repository it touched: ${repos.map(r => `git -C ${ROOT}/${r} diff ${base}...${c.branch}`).join('; ')}.${container ? ' This is a container review: read what is true only between its children, and every child tm render lists as rejected by its own review.' : ''}`
    body += `\nFindings: append numbered findings to tm section ${n.id}:review, one line each; write it even when nothing is open, saying so.`
    // One scratch path per repository: a container review may execute code in several.
    const scratch = repos.map(r => `${WT}/${r}-${n.id}-review`).join(' or ') || `${WT}/<repo>-${n.id}-review`
    body += `\nScratch: a worktree you cut to execute the code goes at ${scratch}, the one named for its repository, detached, and you remove it with git worktree remove before you close the step.`
  } else {
    // A container's step spans repositories, and tm cuts one worktree of its branch in each.
    const trees = Object.entries(c.worktrees || {})
    const where = trees.length > 1
      ? `Worktrees, one per repository, each on branch ${c.branch}, based on ${c.base}: ${trees.map(([r, p]) => `${r} at ${p}`).join('; ')}. Work only there, and never cd in a Bash command.`
      : `Worktree: ${c.worktree} — branch ${c.branch}, based on ${c.base}. Work only there, and never cd in a Bash command.`
    body = c.action === 'fix'
      ? `${where}\nFindings: ${s.outcome === 'merge_failed' ? `the landing failure the latest entry of tm section get ${n.id}:merge records` : `every finding in tm section get ${n.id}:review not recorded as closed`}. Fix each one, commit on the branch, and answer each by number in an appended :report entry.`
      : `${where}\nReport: append to tm section ${n.id}:report before your last commit.`
  }
  const r = await agent(`${head(n, c, fam, c.action)}\n${body}`,
    { label: `${c.action}:${n.id}`, phase: WORK_PHASE[c.action], model: fam, agentType: pickType(type, n.requires) })
  trail.push(`${c.action} on ${fam}: ${r === null ? 'the agent died' : clip(r)}`)
  const after = await read(n)
  if (!after || after.status === CLAIMED[c.action]) await release(n, trail, `the ${c.action} step was left open`, c.token)
  return after
}

const resumes = j => Number(j['result.resumed'] || 0)

// tm parks the lease of a job stopped for an agent and refuses `tm job resume` to anyone a claim
// did not hand it to, so an agent is dispatched only on the claim that handed the job over; any
// other stop returns to run(), whose next claim is that hand-over.
async function land(n, c, trail, handed) {
  let id = c.job
  for (let poll = 0; poll < MAX_POLLS; poll++) {
    const j = await job(n, id, true)
    if (!j) return trail.push(`${c.action}: job ${id} status could not be read`)
    if (j.state === 'running') continue
    // A container lands one repository per job, each chained to the next under the same lease.
    if (j.state === 'succeeded' && j['result.next']) {
      trail.push(`${c.action}: ${id} succeeded; following ${clip(j['result.next'])}`)
      id = q(j['result.next'])
      handed = false
      continue
    }
    if (j.state !== 'needs_agent') return trail.push(`${c.action}: ${j.state}; tm job status ${id} for detail`)
    if (!handed || id !== c.job) return trail.push(`${c.action} stopped for an agent; the next claim hands it over`)
    handed = false
    const fam = c.model
    const r = await agent(`${head(n, c, fam, 'merge')}
Job: ${id}, a ${j.kind} of ${j.repo} onto ${j.target}, stopped at ${j.step}
Worktree: ${j.worktree} — the one tm built for this job. Work only there, and never cd in a Bash command.
Output: ${TM} job status ${id} prints what stopped it.
When this node's own change is at fault, close with ${TM} job resume ${id} ${owner(n, c.token)} --own-defect "<the finding, one line>" instead.`,
      { label: `${c.action}-agent:${n.id}`, phase: 'Land agent', model: fam, agentType: pickType(undefined, n.requires) })
    trail.push(`${c.action} agent: ${r === null ? 'died' : clip(r)}`)
    const k = await job(n, id, false)
    // A job resumed and stopped again is parked and counted by tm itself; releasing it too would
    // count the same stop twice and throw away the job the next agent takes over.
    if (k && k.state === 'needs_agent' && resumes(k) > resumes(j)) {
      return trail.push(`${c.action} stopped again after the agent resumed it; the next claim hands it over`)
    }
    if (!k || k.state === 'needs_agent') return release(n, trail, `the ${c.action} agent left the job stopped`, c.token)
  }
  trail.push(`${c.action}: job ${id} still running after ${MAX_POLLS} waits; a later tick picks it up`)
}

async function run(n) {
  const trail = []
  const end = status => ({ id: n.id, status, trail })
  const s = await read(n)
  if (!s) return end('unreadable')
  if (TERMINAL.has(s.status) || !s.next_action) return end(s.status)
  if (s.next_action === 'merge' && HOLD_MERGE.has(n.id)) return trail.push('merge held by args.holdMerge'), end(s.status)
  const c = await start(n, trail)
  if (!c) return end(s.status)
  if (c.action === 'blocked' && !c.job) return trail.push(`blocked: ${c.reason}`), end('blocked')
  if (c.action in CLAIMED) {
    const after = await work(n, c, s, trail)
    return end(after ? after.status : 'unreadable')
  }
  // A merge claim of a node already MERGING, and every sync claim, is a hand-over of a stopped job.
  const handed = c.action === 'sync' || (c.action === 'merge' && s.status === 'MERGING')
  await land(n, c.action === 'blocked' ? { ...c, action: 'sync' } : c, trail, handed)
  const after = await read(n)
  return end(after ? after.status : 'unreadable')
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
  log(`maxBatch ${A.maxBatch}: left for the next run: ${plan.chosen.slice(A.maxBatch).map(n => n.id).join(', ')}`)
  plan.chosen = plan.chosen.slice(0, A.maxBatch)
}
plan.held.forEach(h => log(`held: ${h}`))
log(`wave: ${plan.chosen.map(n => `${n.id}@${n.action}/${n.model}`).join(', ') || 'nothing claimable'}; ${plan.waiting_for_slot} waiting for a slot`)

const results = await pipeline(plan.chosen, n => run(n))
return { results: results.filter(Boolean), held: plan.held, waitingForSlot: plan.waiting_for_slot }
