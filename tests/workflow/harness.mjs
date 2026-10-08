import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'

export const SOURCE = readFileSync(new URL('../../plugin/workflows/tm-wave.js', import.meta.url), 'utf8')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const META_START = 'export const meta = '

// The Workflow tool reads meta without running the script, so it has to evaluate on its own.
export function meta() {
  const start = SOURCE.indexOf(META_START) + META_START.length
  const end = SOURCE.indexOf('\n}\n', start) + 2
  return Function(`"use strict"; return (${SOURCE.slice(start, end)})`)()
}

// Independent of the script's own djb2: it hashes Buffer bytes, the script a percent-decoded string.
export function djb2(text) {
  let h = 5381
  for (const byte of Buffer.from(text, 'utf8')) h = (Math.imul(h, 33) + byte) >>> 0
  return h
}

// The fake tm's transcript check, computed by shelling out to the stock `cksum` binary rather
// than the script's own cksum(), so a bug shared by both would still show up as a mismatch.
export function realCksum(text) {
  return execFileSync('cksum', { input: Buffer.from(text, 'utf8') }).toString().trim()
}

// A wrong check line that still has cksum's shape, for a test asserting the script rejects it.
const wrongCksum = check => {
  const [crc, len] = check.split(' ')
  return `${Number(crc) + 1} ${len}`
}

// Extracted and evaluated straight out of the script's source, so a test can check its cksum()
// against the real binary without the script exporting anything.
export function scriptCksum(text) {
  const start = SOURCE.indexOf('function cksum(')
  const end = SOURCE.indexOf('\n}\n', start) + 2
  return Function(`"use strict"; return (${SOURCE.slice(start, end)})`)()(text)
}

// A repository as a landed container leaves it: on main, each of `landings` in turn lands by a
// `merge(<lander>): land tm/<lander> on <onto>` merge bringing `<lander>.txt`, after a commit of
// main's own so its first parent differs from the branch; a lander in `elsewhere` names the
// target it maps to instead of `onto`; each of `inner` lands on tm/<id> before tm/<id> lands, as a
// child with merge: parent does. A later commit shares `merge(<id>): `'s prefix, and origin/main,
// origin/<onto> and tm/S1 all point there. Every git call throws on a non-zero exit, so a fixture
// that failed to build fails the test.
export function landedRepo(dir, { id, landings = [id], inner = [], onto = 'main', elsewhere = {} }) {
  const git = (...a) => execFileSync('git', ['-C', dir, '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', ...a], { stdio: 'pipe' })
  const commit = (file, subject) => (writeFileSync(`${dir}/${file}`, subject), git('add', file), git('commit', '-q', '-m', subject))
  const branch = lander => (git('checkout', '-q', '-b', `tm/${lander}`), commit(`${lander}.txt`, `feat(${lander}): its change`))
  mkdirSync(dir, { recursive: true })
  git('init', '-q', '-b', 'main')
  commit('base.txt', 'base')
  for (const lander of landings) {
    branch(lander)
    for (const child of lander === id ? inner : []) {
      branch(child)
      git('checkout', '-q', `tm/${id}`)
      git('merge', '-q', '--no-ff', '-m', `merge(${child}): land tm/${child} on tm/${id}`, `tm/${child}`)
    }
    git('checkout', '-q', 'main')
    commit(`before-${lander}.txt`, 'before')
    git('merge', '-q', '--no-ff', '-m', `merge(${lander}): land tm/${lander} on ${elsewhere[lander] ?? onto}`, `tm/${lander}`)
  }
  commit('after.txt', `merge(${id}): main moved`)
  git('update-ref', 'refs/remotes/origin/main', 'main')
  git('update-ref', `refs/remotes/origin/${onto}`, 'main')
  git('branch', 'tm/S1', 'main')
}

export const json = (value, exit = 0) => ({ text: JSON.stringify(value), exit })

// What `tm wave discover --lines` prints for this batch.
export function discovery(chosen, { held = [], waiting_for_slot = 0 } = {}) {
  const flat = values => (values || []).join(',') || '-'
  return [
    ...chosen.map(n => `N ${n.id} ${n.action} ${n.model} ${n.kind} ${flat(n.repos)} ${flat(n.requires)}`),
    ...held.map(h => `H ${h}`),
    `W ${waiting_for_slot}`,
  ].join('\n')
}

// A scripted reply list is served in order and its last entry repeats, so a test writes only the
// replies that change; an entry that is a function runs when served, for replies with effects.
function queue(list) {
  const items = [...list]
  return () => {
    const entry = items.length > 1 ? items.shift() : items[0]
    return typeof entry === 'function' ? entry() : entry
  }
}

// `parked` names jobs already stopped for an agent before the run starts, as an earlier tick left them.
// `lists` maps a list command, as `task list --plan P1`, to the ids of the rows it prints.
export function makeTm({ chosen = [], nodes = {}, start = {}, job = {}, parked = [], lists = {}, releaseExit = 0, discover, corrupt = () => false } = {}) {
  const state = structuredClone(nodes)
  const starts = Object.fromEntries(Object.entries(start).map(([id, list]) => [id, queue(list)]))
  const jobs = Object.fromEntries(Object.entries(job).map(([id, list]) => [id, queue(list)]))
  // As tm does, a job stopped for an agent is parked, and only a claim made while it is stopped
  // hands it over: `tm job resume` refuses every other caller.
  const stopped = new Set(parked)
  const handed = new Set()
  const parse = text => {
    try {
      return JSON.parse(text)
    } catch (e) {
      return {}
    }
  }
  // A nested name whose run() never populated `result` resolves to null rather than being unknown:
  // only the head segment names a real field.
  const dottedGet = (doc, dotted) => {
    const [head, ...rest] = dotted.split('.')
    let cur = doc[head]
    for (const part of rest) {
      if (cur === null || typeof cur !== 'object' || !(part in cur)) return null
      cur = cur[part]
    }
    return cur
  }
  function answer(inner) {
    // Mirrors the real CLI: only the named fields come back, so a script reading one it never
    // requested gets undefined here exactly as it would against the real `tm task get --fields`.
    let m = inner.match(/^\S+ task get (\S+) --json --fields (\S+)$/)
    if (m) {
      if (!state[m[1]]) return { text: `Task '${m[1]}' not found`, exit: 1 }
      const picked = Object.fromEntries(m[2].split(',').map(f => [f, state[m[1]][f]]))
      return json(picked)
    }
    m = inner.match(/^\S+ ((?:task|plan) list --(?:plan|spec) \S+) --json$/)
    if (m && lists[m[1]]) return json(lists[m[1]].map(id => ({ id, kind: m[1].split(' ')[0] })))
    m = inner.match(/^\S+ task start (\S+) --agent wf-\S+ --session \S+(?: --worktree-dir \S+)? --json$/)
    if (m && starts[m[1]]) {
      const reply = starts[m[1]]()
      const c = parse(reply.text)
      if ((c.action === 'merge' || c.action === 'sync') && stopped.has(c.job)) handed.add(c.job)
      return reply
    }
    m = inner.match(/^\S+ job status (\S+)(?: --wait \d+)? --fields (\S+)$/)
    if (m && jobs[m[1]]) {
      const reply = jobs[m[1]]()
      const full = parse(reply.text)
      if (full.state === 'needs_agent') stopped.add(m[1])
      else stopped.delete(m[1])
      const picked = Object.fromEntries(m[2].split(',').map(f => [f, dottedGet(full, f)]))
      return { ...reply, text: JSON.stringify(picked) }
    }
    throw new Error(`the fake tm has no reply for: ${inner}`)
  }
  return {
    set(id, patch) {
      state[id] = { ...state[id], ...patch }
    },
    // What an agent's `tm job resume <job>` does to the fake: refused unless a claim handed it over.
    resume(id) {
      if (!handed.has(id)) throw new Error(`job ${id} is not handed to an agent: take it with tm task start`)
      handed.delete(id)
      stopped.delete(id)
    },
    reply(cmd) {
      const m = cmd.match(/^out=\$\((.*) 2>&1\); rc=\$\?; /)
      if (m) {
        const { text, exit } = /^\S+ wave discover --lines\b/.test(m[1])
          ? { text: discover ? discover(m[1]) : discovery(chosen), exit: 0 }
          : answer(m[1])
        // corrupt's return is either falsy (send the real check), true (mangle it), or a check
        // line of the caller's own choosing (a djb2, or one hashed over different bytes).
        const bad = corrupt(m[1], text)
        const check = typeof bad === 'string' ? bad : bad ? wrongCksum(realCksum(text)) : realCksum(text)
        return { stdout: `${text}\n__CHECK ${check}\n__EXIT:${exit}\n` }
      }
      if (/^\S+ task release \S+ --agent wf-\S+(?: --token \S+)? >\/dev\/null 2>&1$/.test(cmd)) {
        return { stdout: `__EXIT:${releaseExit}\n` }
      }
      throw new Error(`the fake tm has no reply for: ${cmd}`)
    },
  }
}

const RUNNER = /^\( (.*) \); echo "__EXIT:\$\?"$/m
// op() names the estate by exporting TM_ROOT ahead of its command rather than in the instructions
// text, so the fake tm is answered on the command with that export stripped back off.
const TM_ROOT = /^export TM_ROOT=(\S+); (.*)$/

// `refuse(opts)` true makes that call fail as the harness fails an agent type the session does not
// know; a refused call is never recorded.
export async function runWave({ args, tm, agents = () => 'done', refuse = () => false }) {
  const calls = []
  const logs = []
  const errors = []
  const agent = async (prompt, opts = {}) => {
    if (refuse(opts)) throw new Error(`unknown agent type ${opts.agentType}`)
    const m = prompt.match(RUNNER)
    if (m) {
      const rooted = m[1].match(TM_ROOT)
      const cmd = rooted ? rooted[2] : m[1]
      const reply = tm.reply(cmd)
      // stdout is what a checked read actually copies back, so a test bounds that rather than cmd.
      calls.push({ kind: 'op', cmd, prompt, opts, stdout: reply.stdout })
      return reply
    }
    calls.push({ kind: 'agent', prompt, opts })
    return agents(prompt, opts)
  }
  const pipeline = (items, ...stages) =>
    Promise.all(
      items.map(async (item, index) => {
        let value = item
        for (const stage of stages) {
          try {
            value = await stage(value, item, index)
          } catch (error) {
            errors.push(error)
            return null
          }
        }
        return value
      }),
    )
  const parallel = thunks => Promise.all(thunks.map(t => t().catch(error => (errors.push(error), null))))
  const body = SOURCE.replace(META_START, 'const meta = ')
  const script = new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'log', 'phase', body)
  const result = await script(args, agent, pipeline, parallel, message => logs.push(message), () => {})
  return {
    result,
    logs,
    errors,
    calls,
    ops: calls.filter(c => c.kind === 'op').map(c => c.cmd),
    work: calls.filter(c => c.kind === 'agent'),
  }
}
