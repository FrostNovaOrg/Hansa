import { test, expect, mock } from 'claude-code/testing'
import { register } from '../hooks/register.js'

const SETTINGS = '/desk/settings.json'
const MIN = 60_000
const THRESHOLDS = { idle_at: 400_000, long_idle_at: 200_000, long_idle_min: 50, working_at: 550_000 }
// The desk's settings file, as hansa's page saves it: the thresholds among the desk's other settings
const desk = (thresholds: Record<string, unknown>) => JSON.stringify({
  version: 1, concurrency: 2, coordinator: { provider: 'claude', model: 'opus' },
  coordinator_compact: { ...THRESHOLDS, ...thresholds },
})

// Claude Code beneath the mod: a context whose size the test sets, the desk's settings file in memory, and what the
// mod asked of it and logged. now: when the test starts; clear: the window hansa hands over, if any
function setup(on, tokens: { value: number }, thresholds: Record<string, unknown> = {},
  { now = 1_760_000_000_000, clear = undefined as string | undefined } = {}) {
  const clock = mock.clock(on, { now })
  mock.env(on, { HANSA_MOD_SETTINGS: SETTINGS, ...(clear === undefined ? {} : { HANSA_MOD_CLEAR_DAILY: clear }) })
  const files: Record<string, string> = { [SETTINGS]: desk(thresholds) }
  const calls = { compact: 0, refuse: '', skip: '', shrinks: true, reads: 0, writes: 0, logs: [] as string[],
    clears: [] as { command: string, args?: string, at: number }[], clearFails: 0,
    during: null as null | (($) => Promise<unknown>) }
  on('fs.read', async ($, e) => {
    calls.reads += 1 // one each decision
    if (!(e.path in files)) throw new Error(`ENOENT: ${e.path}`)
    return { value: files[e.path] }
  })
  on('fs.write', async () => {
    calls.writes += 1
    return { value: undefined }
  })
  on('session.usage', async () => ({ value: { context: { tokens: tokens.value, window: 1_000_000 }, rateLimits: [] } }))
  on('session.compact', async () => {
    calls.compact += 1
    if (calls.refuse) throw new Error(calls.refuse)
    if (calls.skip) return { skip: calls.skip }
    const before = tokens.value
    if (calls.shrinks) tokens.value = 60_000
    return { messages: [{ role: 'user', text: '(summary)', toolUses: [] }], tokensBefore: before, tokensAfter: tokens.value }
  })
  on('command.run', async ($, e) => {
    calls.clears.push({ command: e.command, args: e.args, at: clock.now() })
    if (calls.during) await calls.during($) // what comes while the clear has not returned
    if (calls.clearFails > 0) { // the next ones fail: the engine's command.run rejects
      calls.clearFails -= 1
      throw new Error('not now')
    }
    return { text: '' }
  })
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('classic.Stop', async () => ({}))
  on('turn.complete', async ($, e) => ({ text: e.answer }))
  on('ui.log', async ($, e) => {
    calls.logs.push(JSON.stringify(e))
    return { value: undefined }
  })
  const set = (x: Record<string, unknown>) => { files[SETTINGS] = desk(x) }
  return { clock, calls, files, set }
}

const done = (turnId: string, extra: Record<string, unknown> = {}) => ({
  answer: 'ok', durationMs: 1000, isAborted: false, turnId, reason: 'answer' as const, ...extra,
})
const stopped = (extra: Record<string, unknown> = {}) => ({
  stop_hook_active: false, background_tasks: [], session_crons: [], ...extra,
})

// One turn of the main conversation that ends with an answer, its Stop listing what the test gives
async function turn($, id: string, stop: Record<string, unknown> = {}) {
  await $.turn.start({ text: 'work', turnId: id })
  await $.classic.Stop(stopped(stop))
  await $.turn.complete(done(id))
}

// -- idle: at or above idle_at, a minute with no new turn ----------------------------------------------------------

test('idle above the threshold: it compacts once, after a minute', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = setup(on, tokens)
  await turn($, 't1')
  await clock.advance(59_000)
  expect(calls.compact).toBe(0)
  await clock.advance(2_000)
  expect(calls.compact).toBe(1)
  await clock.advance(5 * MIN)
  expect(calls.compact).toBe(1)
})

// The exact edges: at the threshold itself, and at 60 seconds and 50 minutes to the millisecond

test('idle: at exactly 400,000 it compacts at exactly 60 seconds, not a millisecond before', async ($, on) => {
  const tokens = { value: 400_000 }
  const { clock, calls } = setup(on, tokens, { long_idle_at: 1_000_000 })
  await turn($, 't1')
  await clock.advance(59_999)
  expect(calls.compact).toBe(0)
  await clock.advance(1)
  expect(calls.compact).toBe(1)
})

test('idle: at 399,999 it does not compact at 60 seconds', async ($, on) => {
  const tokens = { value: 399_999 }
  const { clock, calls } = setup(on, tokens, { long_idle_at: 1_000_000 })
  await turn($, 't1')
  await clock.advance(120 * MIN)
  expect(calls.compact).toBe(0)
})

test('long idle: at exactly 200,000 it compacts at exactly 50 minutes, not a millisecond before', async ($, on) => {
  const tokens = { value: 200_000 }
  const { clock, calls } = setup(on, tokens)
  await turn($, 't1')
  await clock.advance(50 * MIN - 1)
  expect(calls.compact).toBe(0)
  await clock.advance(1)
  expect(calls.compact).toBe(1)
})

for (const shrinks of [true, false]) { // also when the compact leaves the context above both thresholds
  test(`at 400,000 or more: it compacts at 60 seconds, and nothing happens again at 50 minutes${shrinks ? '' :
    ' (the compact left it above both)'}`, async ($, on) => {
    const tokens = { value: 450_000 }
    const { clock, calls } = setup(on, tokens)
    calls.shrinks = shrinks
    await turn($, 't1')
    await clock.advance(61_000)
    expect(calls.compact).toBe(1)
    await clock.advance(55 * MIN)
    expect(calls.compact).toBe(1)
  })
}

test('the Stop may come after the turn ends: it compacts all the same', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = setup(on, tokens)
  await $.turn.start({ text: 'work', turnId: 't1' })
  await $.turn.complete(done('t1'))
  await clock.advance(120_000)
  expect(calls.compact).toBe(0) // no Stop yet: whether background work waits is not known
  await $.classic.Stop(stopped())
  await clock.advance(59_000)
  expect(calls.compact).toBe(0) // idle from the Stop, the later of the two
  await clock.advance(2_000)
  expect(calls.compact).toBe(1)
})

test('below both thresholds: no compact, however long', async ($, on) => {
  const tokens = { value: 150_000 }
  const { clock, calls } = setup(on, tokens)
  await turn($, 't1')
  await clock.advance(120 * MIN)
  expect(calls.compact).toBe(0)
})

// -- long idle: at or above long_idle_at, long_idle_min minutes with no new turn -----------------------------------

test('long idle: above 200,000 it compacts after 50 minutes, not before', async ($, on) => {
  const tokens = { value: 300_000 }
  const { clock, calls } = setup(on, tokens)
  await turn($, 't1')
  await clock.advance(2 * MIN)
  expect(calls.compact).toBe(0) // under the idle threshold: not at a minute
  await clock.advance(47 * MIN)
  expect(calls.compact).toBe(0) // 49 minutes
  await clock.advance(MIN + 1_000)
  expect(calls.compact).toBe(1)
  await clock.advance(120 * MIN)
  expect(calls.compact).toBe(1)
})

test('long idle: below 200,000 it does not compact at 50 minutes', async ($, on) => {
  const tokens = { value: 199_999 }
  const { clock, calls } = setup(on, tokens)
  await turn($, 't1')
  await clock.advance(51 * MIN)
  expect(calls.compact).toBe(0)
})

for (const [rule, value, after] of [['the long idle', 300_000, 50 * MIN], ['the idle', 450_000, MIN]] as const) {
  test(`a new turn cancels ${rule}'s wait`, async ($, on) => {
    const tokens = { value }
    const { clock, calls } = setup(on, tokens)
    await turn($, 't1')
    await clock.advance(after - 10_000)
    await $.turn.start({ text: 'hansa posted', turnId: 't2' })
    await clock.advance(5_000)
    await $.classic.Stop(stopped())
    await $.turn.complete(done('t2'))
    const reads = calls.reads
    await clock.advance(10_000) // when the first wait would have ended: no decision, no compact
    expect(calls.reads).toBe(reads)
    expect(calls.compact).toBe(0)
    await clock.advance(after - 20_000)
    expect(calls.compact).toBe(0) // counted afresh from that turn's end
    await clock.advance(11_000)
    expect(calls.compact).toBe(1)
  })
}

// Every way back to idle decides afresh and cancels the wait first, so only the wait itself shows that a new turn
// cancelled it at once: the mod's handlers, given a $ whose waits say when they are cancelled
test('a new turn cancels the wait at once, as it starts', async () => {
  const handlers: Record<string, Function> = {}
  register((name: string, fn: Function) => {
    handlers[name] = fn
    return { catch: () => undefined }
  })
  const waits = { set: 0, cancelled: 0 }
  const $ = {
    clock: { now: async () => 1_000, after: () => ((waits.set += 1), { cancel: () => { waits.cancelled += 1 } }) },
    env: { get: async () => SETTINGS },
    fs: { read: async () => desk({}) },
    session: { usage: async () => ({ context: { tokens: 450_000 } }), compact: async () => ({}) },
    ui: { log: async () => undefined },
  }
  const next = async (e: unknown) => e
  await handlers['turn.start']($, { text: 'work', turnId: 't1' }, next)
  await handlers['classic.Stop']($, stopped(), next)
  await handlers['turn.complete']($, done('t1'), next)
  expect(waits).toEqual({ set: 1, cancelled: 0 }) // waiting for the minute
  await handlers['turn.start']($, { text: 'hansa posted', turnId: 't2' }, next)
  expect(waits).toEqual({ set: 1, cancelled: 1 })
})

// -- thresholds: read at each decision -----------------------------------------------------------------------------

test('a threshold changed during a wait is the one used when the wait ends', async ($, on) => {
  const tokens = { value: 300_000 }
  const { clock, calls, set } = setup(on, tokens)
  await turn($, 't1')
  await clock.advance(10 * MIN)
  set({ long_idle_min: 90 }) // the long idle made longer while it waits: not at 50 minutes, at 90
  await clock.advance(41 * MIN)
  expect(calls.compact).toBe(0)
  await clock.advance(39 * MIN)
  expect(calls.compact).toBe(1)
  // raised above the context while it waits: no compact when the wait ends
  tokens.value = 300_000
  set({})
  await turn($, 't2')
  await clock.advance(10 * MIN)
  set({ long_idle_at: 350_000 })
  await clock.advance(45 * MIN)
  expect(calls.compact).toBe(1)
  // the idle threshold lowered under the context while it waits for the long idle, and the long idle's raised above
  // it: when that wait ends, only the idle rule can compact
  set({})
  await turn($, 't3')
  await clock.advance(10 * MIN)
  set({ idle_at: 250_000, long_idle_at: 350_000 })
  await clock.advance(39 * MIN)
  expect(calls.compact).toBe(1) // decided when the wait ends, not when the file changes
  await clock.advance(2 * MIN)
  expect(calls.compact).toBe(2)
})

test('the idle threshold raised while it waits: decided again when the wait ends, no compact', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls, set } = setup(on, tokens)
  await turn($, 't1')
  await clock.advance(30_000)
  set({ idle_at: 500_000, long_idle_at: 500_000 })
  await clock.advance(60 * MIN)
  expect(calls.compact).toBe(0)
})

test('changed between turns: the next turn decides by the new thresholds', async ($, on) => {
  const tokens = { value: 300_000 }
  const { clock, calls, set } = setup(on, tokens, { long_idle_at: 400_000 })
  await turn($, 't1')
  await clock.advance(120 * MIN)
  expect(calls.compact).toBe(0)
  set({ idle_at: 250_000 })
  await turn($, 't2')
  await clock.advance(61_000)
  expect(calls.compact).toBe(1)
})

// Each default, and each value out of range, in a case only its own rule can satisfy: the other rule is set out of reach
// (idle_at or long_idle_at at 1,000,000), so a wrong default cannot pass through it
const OFF = 1_000_000
for (const [what, cc, tokens, notBefore, at] of [
  ['idle_at absent: 400,000', { long_idle_at: OFF }, 400_000, MIN - 1, MIN],
  ['idle_at absent: not 399,999', { long_idle_at: OFF }, 399_999, 120 * MIN, null],
  ['long_idle_at absent: 200,000', { idle_at: OFF, long_idle_min: 50 }, 200_000, 50 * MIN - 1, 50 * MIN],
  ['long_idle_at absent: not 199,999', { idle_at: OFF, long_idle_min: 50 }, 199_999, 120 * MIN, null],
  ['long_idle_min absent: 50 minutes', { idle_at: OFF, long_idle_at: 200_000 }, 300_000, 50 * MIN - 1, 50 * MIN],
  ['idle_at out of range (5)', { idle_at: 5, long_idle_at: OFF }, 300_000, 120 * MIN, null],
  ['long_idle_at not a number (\'250000\')', { idle_at: OFF, long_idle_at: '250000' }, 200_000, 50 * MIN - 1, 50 * MIN],
  ['long_idle_min out of range (0)', { idle_at: OFF, long_idle_min: 0 }, 300_000, 50 * MIN - 1, 50 * MIN],
] as const) {
  test(`a threshold absent from the file, or out of the page's range, is its default: ${what}`, async ($, on) => {
    const value = { value: tokens }
    const { clock, calls, files } = setup(on, value)
    files[SETTINGS] = JSON.stringify({ version: 1, coordinator_compact: cc }) // nothing else of the three
    await turn($, 't1')
    await clock.advance(notBefore)
    expect(calls.compact).toBe(0)
    if (at !== null) {
      await clock.advance(at - notBefore)
      expect(calls.compact).toBe(1)
    }
  })
}

test('a desk saved before these settings: the defaults', async ($, on) => {
  const tokens = { value: 300_000 } // under the idle default: only the long idle can compact, at 50 minutes
  const { clock, calls, files } = setup(on, tokens)
  files[SETTINGS] = JSON.stringify({ version: 1 })
  await turn($, 't1')
  await clock.advance(50 * MIN - 1)
  expect(calls.compact).toBe(0)
  await clock.advance(1)
  expect(calls.compact).toBe(1)
})

test('a settings file that cannot be read or parsed: no compact while idle, and nothing of it said', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls, files } = setup(on, tokens)
  for (const text of ['root:x:0:0:root:/root:/bin/sh', '["sk-secret-1234"]', 'null', '"sk-secret-1234"']) {
    files[SETTINGS] = text
    await turn($, `t-${text}`)
    await clock.advance(60 * MIN)
  }
  delete files[SETTINGS]
  await turn($, 't2')
  await clock.advance(60 * MIN)
  expect(calls.compact).toBe(0)
  expect(calls.logs).toEqual([]) // nothing said of a file it could not read or parse: what is wrong may quote it
  // the desk's other settings are never repeated: only the three numbers are used
  files[SETTINGS] = JSON.stringify({ providers: { x: { key: 'sk-secret-1234' } }, coordinator_compact: THRESHOLDS })
  await turn($, 't3')
  await clock.advance(61_000)
  expect(calls.compact).toBe(1)
  expect(calls.logs.join('\n')).not.toContain('sk-secret')
  expect(calls.logs.join('\n')).not.toContain('root:x')
  expect(calls.writes).toBe(0) // it writes no file of its own
})

// -- what idle means -----------------------------------------------------------------------------------------------

test('lowered while a turn runs, or with background work waiting: no compact', async ($, on) => {
  const tokens = { value: 300_000 }
  const { clock, calls, set } = setup(on, tokens)
  await $.turn.start({ text: 'work', turnId: 't1' })
  set({ idle_at: 250_000 })
  await clock.advance(3 * MIN)
  expect(calls.compact).toBe(0)
  await $.classic.Stop(stopped({ background_tasks: [{ id: 'b1', type: 'local_bash', status: 'running', description: 'x' }] }))
  await $.turn.complete(done('t1'))
  await clock.advance(60 * MIN)
  expect(calls.compact).toBe(0)
})

test('not while a turn runs: a subagent ending inside it counts for nothing', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = setup(on, tokens)
  await $.turn.start({ text: 'work', turnId: 't1' })
  await $.turn.complete(done('sub1', { agentId: 'agent-1' }))
  await $.classic.Stop(stopped({ agent_id: 'agent-1' }))
  await clock.advance(2 * MIN)
  expect(calls.compact).toBe(0)
  await $.classic.Stop(stopped()) // the main conversation's Stop, then a subagent ends while its turn still runs
  await $.turn.complete(done('sub2', { agentId: 'agent-2' }))
  await clock.advance(60 * MIN)
  expect(calls.compact).toBe(0)
})

test('a subagent ending while the main conversation waits counts for nothing, even with work of its own', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = setup(on, tokens)
  await turn($, 't1')
  await clock.advance(30_000) // idle, waiting for the minute
  await $.classic.Stop(stopped({ agent_id: 'agent-1',
    background_tasks: [{ id: 'b1', type: 'local_bash', status: 'running', description: 'x' }] }))
  await clock.advance(31_000)
  expect(calls.compact).toBe(1)
})

test('an interrupted or failed turn: no compact', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = setup(on, tokens)
  await $.turn.start({ text: 'work', turnId: 't1' })
  await $.classic.Stop(stopped())
  await $.turn.complete(done('t1', { reason: 'aborted', isAborted: true }))
  await clock.advance(60 * MIN)
  await $.turn.start({ text: 'work', turnId: 't2' })
  await $.classic.Stop(stopped())
  await $.turn.complete(done('t2', { reason: 'error' }))
  await clock.advance(60 * MIN)
  expect(calls.compact).toBe(0)
})

test('background tasks or session crons waiting: no compact', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = setup(on, tokens)
  await turn($, 't1', { background_tasks: [{ id: 'b1', type: 'local_bash', status: 'running', description: 'hansa wait' }] })
  await clock.advance(60 * MIN)
  expect(calls.compact).toBe(0)
  await turn($, 't2', { session_crons: [{ id: 'c1', schedule: '*/5 * * * *', recurring: true, prompt: 'look' }] })
  await clock.advance(60 * MIN)
  expect(calls.compact).toBe(0)
  await turn($, 't3')
  await clock.advance(61_000)
  expect(calls.compact).toBe(1)
})

// -- a compact skipped or refused ----------------------------------------------------------------------------------

for (const how of ['skipped', 'refused']) {
  test(`a compact ${how}: not asked for again until a new turn starts, whatever Stop comes between`, async ($, on) => {
    const tokens = { value: 450_000 }
    const { clock, calls } = setup(on, tokens)
    if (how === 'skipped') calls.skip = 'not now'
    else calls.refuse = 'a turn is running'
    await turn($, 't1')
    await clock.advance(61_000)
    expect(calls.compact).toBe(1)
    await $.classic.Stop(stopped()) // two Stops with no turn between them
    await clock.advance(61_000)
    await $.classic.Stop(stopped())
    await $.turn.complete(done('t1')) // and the turn's end again, with no new turn
    await clock.advance(60 * MIN) // the long idle's time passes too
    expect(calls.compact).toBe(1)
    calls.skip = ''
    calls.refuse = ''
    await turn($, 't2')
    await clock.advance(61_000)
    expect(calls.compact).toBe(2)
  })
}

// -- the daily clear: once in each window hansa hands over, idle for 5 minutes ------------------------------------

// A local time (the window is in local time): the day after 2026-09-27 (0 for that day) at hour h, minute m
const local = (day: number, h: number, m = 0) => new Date(2026, 8, 27 + day, h, m).getTime()
const STARTED = { cwd: '/desk', surface: 'terminal' as const, isInteractive: true }

// The session starts at `day h:m`, with window `clear`: the mod's state is made then
async function started($, on, clear: string | undefined, day: number, h: number, m = 0,
  tokens = { value: 150_000 }, thresholds: Record<string, unknown> = {}) {
  const s = setup(on, tokens, thresholds, { now: local(day, h, m), clear })
  await $.session.start(STARTED)
  return s
}

// Move the clock to `day h:m`
const until = (clock, day: number, h: number, m = 0) => clock.advance(local(day, h, m) - clock.now())

test('inside the window, idle for 5 minutes: it clears, as /clear with no argument', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 3)
  await until(clock, 0, 4, 10)
  await turn($, 't1')
  await clock.advance(5 * MIN - 1)
  expect(calls.clears).toEqual([])
  await clock.advance(1)
  expect(calls.clears.length).toBe(1)
  expect(calls.clears[0].command).toBe('clear')
  expect(calls.clears[0].args || '').toBe('') // nothing after the name
  expect(calls.clears[0].at).toBe(local(0, 4, 15))
})

test('once per window: no other clear in it, however long it stays idle or however many turns end', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 3)
  await until(clock, 0, 4, 10)
  await turn($, 't1')
  await until(clock, 0, 5)
  expect(calls.clears.length).toBe(1)
  await turn($, 't2')
  await until(clock, 0, 8, 59)
  expect(calls.clears.length).toBe(1)
  // the next day's window: still idle, so it clears as that window opens
  await until(clock, 1, 3, 59)
  expect(calls.clears.length).toBe(1)
  await until(clock, 1, 4)
  expect(calls.clears.length).toBe(2)
  expect(calls.clears[1].at).toBe(local(1, 4))
})

test('outside the window: no clear, whatever the idle; it waits for the window to open', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 9)
  await until(clock, 0, 10)
  await turn($, 't1')
  await until(clock, 1, 3, 59)
  expect(calls.clears).toEqual([])
  await until(clock, 1, 4)
  expect(calls.clears.map(c => c.at)).toEqual([local(1, 4)])
})

test('a window that ends before 5 minutes of idle: no clear in it', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 3)
  await until(clock, 0, 8, 57)
  await turn($, 't1')
  await until(clock, 0, 23)
  expect(calls.clears).toEqual([])
})

test('a window past midnight (22-03): opened the day before, it is the same window after midnight', async ($, on) => {
  const { clock, calls } = await started($, on, '22-03', 0, 21)
  await until(clock, 0, 21, 59)
  await $.turn.start({ text: 'a long turn', turnId: 't1' }) // runs over the window's opening and past midnight
  await until(clock, 1, 0, 10)
  await $.classic.Stop(stopped())
  await $.turn.complete(done('t1'))
  await until(clock, 1, 0, 15)
  expect(calls.clears.map(c => c.at)).toEqual([local(1, 0, 15)])
  await turn($, 't2')
  await until(clock, 1, 2, 59)
  expect(calls.clears.length).toBe(1) // the same window: cleared already
  await turn($, 't3')
  await until(clock, 1, 21, 59)
  expect(calls.clears.length).toBe(1) // outside, between 03:00 and 22:00
  await until(clock, 1, 22)
  expect(calls.clears.map(c => c.at)).toEqual([local(1, 0, 15), local(1, 22)])
  await turn($, 't4')
  await until(clock, 2, 1)
  expect(calls.clears.length).toBe(2)
})

test('a turn started within the 5 minutes: counted afresh from its end (hansa posted)', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 3)
  await until(clock, 0, 4, 10)
  await turn($, 't1')
  await clock.advance(4 * MIN)
  await $.turn.start({ text: 'hansa posted', turnId: 't2' })
  await clock.advance(30_000)
  await $.classic.Stop(stopped())
  await $.turn.complete(done('t2'))
  await clock.advance(5 * MIN - 1)
  expect(calls.clears).toEqual([])
  await clock.advance(1)
  expect(calls.clears.map(c => c.at)).toEqual([local(0, 4, 19) + 30_000])
})

test('not while a turn runs, nor after an interrupted one', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 3)
  await until(clock, 0, 4, 10)
  await $.turn.start({ text: 'work', turnId: 't1' })
  await until(clock, 0, 5)
  expect(calls.clears).toEqual([])
  await $.classic.Stop(stopped())
  await $.turn.complete(done('t1', { reason: 'aborted', isAborted: true }))
  await until(clock, 0, 6)
  expect(calls.clears).toEqual([])
})

for (const [what, stop] of [
  ['background tasks', { background_tasks: [{ id: 'b1', type: 'local_bash', status: 'running', description: 'hansa wait' }] }],
  ['session crons', { session_crons: [{ id: 'c1', schedule: '*/5 * * * *', recurring: true, prompt: 'look' }] }],
] as const) {
  test(`${what} in the last Stop: no clear until a Stop lists none`, async ($, on) => {
    const { clock, calls } = await started($, on, '04-09', 0, 3)
    await until(clock, 0, 4, 10)
    await turn($, 't1', stop)
    await until(clock, 0, 5, 10)
    expect(calls.clears).toEqual([])
    await turn($, 't2')
    await clock.advance(5 * MIN)
    expect(calls.clears.map(c => c.at)).toEqual([local(0, 5, 15)])
  })
}

test('a state made inside the window: no clear in that window; the next window clears', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 4, 30) // started, or the mod reloaded, at 04:30
  await turn($, 't1')
  await until(clock, 0, 8, 59)
  expect(calls.clears).toEqual([])
  await until(clock, 1, 4)
  expect(calls.clears.map(c => c.at)).toEqual([local(1, 4)])
})

test('a state made with no session.start (a reload): made at the first turn', async ($, on) => {
  const { clock, calls } = setup(on, { value: 150_000 }, {}, { now: local(0, 4, 30), clear: '04-09' })
  await turn($, 't1')
  await until(clock, 0, 8, 59)
  expect(calls.clears).toEqual([])
})

test('a state made mid-turn (a reload with no session.start nor turn.start): no clear in that window', async ($, on) => {
  const { clock, calls } = setup(on, { value: 150_000 }, {}, { now: local(0, 4, 30), clear: '04-09' })
  await $.classic.Stop(stopped()) // the first it hears of the session: the end of a turn it did not see start
  await $.turn.complete(done('t1'))
  await until(clock, 0, 8, 59)
  expect(calls.clears).toEqual([])
  await until(clock, 1, 4)
  expect(calls.clears.map(c => c.at)).toEqual([local(1, 4)])
})

test('a state made mid-turn after this window\'s clear: not a second clear in it', async ($, on) => {
  const { clock, calls } = setup(on, { value: 150_000 }, {}, { now: local(0, 6), clear: '04-09' })
  await $.classic.Stop(stopped())
  await $.turn.complete(done('t1'))
  await until(clock, 0, 8, 59)
  expect(calls.clears).toEqual([])
})

test('a state made at the very hour the window opens: made inside it, no clear in it', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 4)
  await turn($, 't1')
  await until(clock, 0, 8, 59)
  expect(calls.clears).toEqual([])
})

test('a Stop while the clear has not returned: nothing else is decided meanwhile', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = await started($, on, '04-09', 0, 3, 0, tokens)
  await until(clock, 0, 4, 10)
  await turn($, 't1')
  calls.during = async () => { // the clear waits on Claude Code; a Stop with nothing waiting comes meanwhile
    calls.during = null
    await $.classic.Stop(stopped())
    expect(calls.compact).toBe(0)
  }
  await clock.advance(5 * MIN)
  expect(calls.clears.length).toBe(1)
  expect(calls.compact).toBe(0) // the context is still large: compaction counts from the clear
  await clock.advance(MIN)
  expect(calls.compact).toBe(1)
})

for (const clear of [undefined, '', '9am', '04-04', '25-03']) {
  test(`no window handed over, or one that is not a window (${clear}): never a clear`, async ($, on) => {
    const { clock, calls } = await started($, on, clear, 0, 0)
    await turn($, 't1')
    await until(clock, 1, 23)
    expect(calls.clears).toEqual([])
  })
}

test('a clear that fails: not tried again until another turn starts; then it is', async ($, on) => {
  const { clock, calls } = await started($, on, '04-09', 0, 3)
  calls.clearFails = 1 // only the first: one tried again at once would clear
  await until(clock, 0, 4, 10)
  await turn($, 't1')
  await until(clock, 0, 5)
  expect(calls.clears.length).toBe(1)
  await turn($, 't2')
  await clock.advance(5 * MIN)
  expect(calls.clears.length).toBe(2)
  expect(calls.logs[0]).toContain('hansa: daily clear not run:')
})

// -- idle compaction and the daily clear ----------------------------------------------------------------------------

test('inside the window, idle compaction waits for the clear; after it, compaction is as before', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = await started($, on, '04-09', 0, 3, 0, tokens)
  await until(clock, 0, 4, 10)
  await turn($, 't1')
  await clock.advance(5 * MIN - 1)
  expect(calls.compact).toBe(0) // past the idle minute: it waits for the clear
  await clock.advance(1)
  expect(calls.clears.length).toBe(1)
  expect(calls.compact).toBe(0)
  await clock.advance(MIN - 1) // the context is still large: the idle rule counts from the clear
  expect(calls.compact).toBe(0)
  await clock.advance(1)
  expect(calls.compact).toBe(1)
})

test('outside the window, idle compaction is as before', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = await started($, on, '04-09', 0, 9, 0, tokens)
  await until(clock, 0, 10)
  await turn($, 't1')
  await clock.advance(MIN)
  expect(calls.compact).toBe(1)
  expect(calls.clears).toEqual([])
  // and the long idle, before a window opens
  tokens.value = 300_000
  await until(clock, 0, 12)
  await turn($, 't2')
  await clock.advance(50 * MIN - 1)
  expect(calls.compact).toBe(1)
  await clock.advance(1)
  expect(calls.compact).toBe(2)
})

test('a compact before the window does not keep it from clearing when the window opens', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = await started($, on, '04-09', 0, 2, 0, tokens)
  await until(clock, 0, 3, 30)
  await turn($, 't1')
  await until(clock, 0, 3, 31)
  expect(calls.compact).toBe(1)
  await until(clock, 0, 4)
  expect(calls.clears.map(c => c.at)).toEqual([local(0, 4)])
})

test('a window it does not clear in (its state made there): compaction is as before', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = await started($, on, '04-09', 0, 4, 30, tokens)
  await turn($, 't1')
  await clock.advance(MIN)
  expect(calls.compact).toBe(1)
  expect(calls.clears).toEqual([])
})

test('the window ends before the clear: compaction is as before from its end', async ($, on) => {
  const tokens = { value: 450_000 }
  const { clock, calls } = await started($, on, '04-09', 0, 3, 0, tokens)
  await until(clock, 0, 8, 57)
  await turn($, 't1')
  await until(clock, 0, 8, 59)
  expect(calls.compact).toBe(0)
  await until(clock, 0, 9)
  expect(calls.compact).toBe(1)
  expect(calls.clears).toEqual([])
})
