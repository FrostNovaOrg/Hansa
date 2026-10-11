// hansa's mod for the coordinator's session. `hansa coordinator` loads a copy of this folder with --plugin-dir,
// for that session only; workers and design sessions never load it.
//
// Compaction when idle: once a turn of the main conversation has ended with an answer and the Stop that ended it
// listed no background tasks and no session crons, the session is idle. Then it compacts after IDLE_MS with no new
// turn when the context is at or above hansa's idle_at, while the whole context is still in the prompt cache; and
// after long_idle_min minutes when it is at or above long_idle_at, before the prompt cache (an hour) lapses, so the
// next turn starts from a short context instead of reading a long one uncached. A new turn cancels any wait. While
// the session works it never interrupts: Claude Code's own auto-compact, at the window hansa gives it with
// --autocompact, does that.
//
// hansa gives it, in the environment, HANSA_MOD_SETTINGS: the desk's own settings file, the one hansa's page saves.
// At each decision it reads there the three numbers under "coordinator_compact" (idle_at, long_idle_at in tokens,
// long_idle_min in minutes), so a change on the page holds from the next decision; one absent, or out of the page's
// range, is its default. It uses only those numbers: nothing else of the file is kept, logged or repeated. It writes
// no file of its own, and keeps nothing in $.store.
//
// The daily clear: with `hansa coordinator --clear-daily HH-HH`, hansa also gives it HANSA_MOD_CLEAR_DAILY, a window of
// hours in local time (22-03 runs past midnight), for that start only. Once in each window it clears the session
// (/clear, as if typed) when the main conversation has been idle for CLEAR_IDLE_MS: hansa's post and anyone's message
// start a turn, so that is also that long with no post. Not in a window this state was made in: a session started then
// has just been told what to do, and a reload of the mod, which forgets what it knew, does not clear twice. Inside a
// window it is still to clear in, idle compaction waits for the clear; outside one, or once cleared, it is as above.
// hansa's SessionStart hook for clear then prints hansa status into the fresh context, and records the clear.

const IDLE_MS = 60_000 // shorter than the prompt cache's lifetime
const MINUTE_MS = 60_000
const CLEAR_IDLE_MS = 5 * MINUTE_MS
// Each threshold's default and range, as hansa's (desk.py: COMPACT_DEFAULTS, COMPACT_MOST)
const THRESHOLDS = {
  idle_at: [400_000, 100_000, 1_000_000],
  long_idle_at: [200_000, 100_000, 1_000_000],
  long_idle_min: [50, 1, 240],
}

// What one loaded copy of the mod knows
function fresh() {
  return {
    busy: false, // a turn of the main conversation is running
    answered: false, // the last such turn ended with an answer
    stop: null, // what the Stop since that turn started listed: { background, crons }; null before it came
    since: null, // when the session became idle (ms); null while it is not
    pending: null, // the wait for the next decision, not yet ended
    tried: false, // a compact was asked for since the last turn started: no other until another starts
    made: null, // when this state was made (ms): at the session's start, or at a reload of the mod
    cleared: null, // when the window last cleared in opened (ms)
    clearing: false, // a clear was asked for and has not returned
    clearFailed: false, // a clear failed since the last turn started: no other until another starts
  }
}

async function born($, st) {
  if (st.made === null) st.made = await $.clock.now()
}

function cancel(st) {
  if (st.pending) {
    st.pending.cancel()
    st.pending = null
  }
}

function idle(st) {
  return !st.busy && st.answered && st.stop !== null && !st.stop.background && !st.stop.crons
}

function said(err) {
  return String(err && err.message ? err.message : err).slice(0, 300)
}

// The thresholds in the desk's settings file as it is now: { idle_at, long_idle_at, long_idle_min }, or null when
// the file cannot be read or parsed (then nothing compacts while idle)
async function thresholds($) {
  let x
  try {
    const path = await $.env.get('HANSA_MOD_SETTINGS')
    if (!path) return null
    x = JSON.parse(await $.fs.read(path))
  } catch {
    return null // what the read or the parse said is not repeated: it may quote the file
  }
  if (!x || typeof x !== 'object' || Array.isArray(x)) return null
  const cc = x.coordinator_compact && typeof x.coordinator_compact === 'object' ? x.coordinator_compact : {}
  const t = {}
  for (const [k, [byDefault, least, most]] of Object.entries(THRESHOLDS)) {
    const v = cc[k]
    t[k] = Number.isInteger(v) && v >= least && v <= most ? v : byDefault
  }
  return t
}

// The window HANSA_MOD_CLEAR_DAILY gives, by hansa's rule (plugin.py: parse_window): [first hour, hour it ends], or
// null for none, or for what is not a window
function window(text) {
  const m = /^([0-9]{1,2})-([0-9]{1,2})$/.exec(String(text ?? '').trim())
  const a = m ? Number(m[1]) : -1
  const b = m ? Number(m[2]) || 24 : -1
  return a >= 0 && a <= 23 && b >= 1 && b <= 24 && a !== b ? [a, b] : null
}

// Where a time stands with the window, in local time: { opened, ends } of the window it is in (ms), else { opens }
function where([a, b], now) {
  const d = new Date(now)
  const at = (days, hour) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + days, hour).getTime()
  for (const day of [-1, 0]) { // yesterday's window may run past midnight
    const opened = at(day, a)
    const ends = at(day + (b <= a ? 1 : 0), b)
    if (opened <= now && now < ends) return { opened, ends }
  }
  return { opens: at(0, a) > now ? at(0, a) : at(1, a) }
}

// The daily clear at now: null with no window; { opened, endsIn } inside a window still to clear in; else { opensIn }
// until the next window it may clear in (ms). Not one it cleared in, nor one its state was made in
function daily(win, now, st) {
  if (!win) return null
  const w = where(win, now)
  if (w.opens !== undefined) return { opensIn: w.opens - now }
  if (st.cleared !== w.opened && st.made < w.opened) return { opened: w.opened, endsIn: w.ends - now }
  const next = where(win, w.ends)
  return { opensIn: (next.opened ?? next.opens) - now }
}

// The one decision, from the context's tokens, how long the session has been idle (ms), the thresholds (null: no
// compact) and the daily clear (null: none): 'clear' or 'compact' now; { at } to decide again once it has been idle
// that long (ms); or null, nothing. Inside a window still to clear in, only the clear: compaction waits for it
function decide(tokens, idleMs, t, clear) {
  if (clear && clear.endsIn !== undefined) {
    if (idleMs >= CLEAR_IDLE_MS) return 'clear'
    return { at: Math.min(CLEAR_IDLE_MS, idleMs + clear.endsIn) }
  }
  const due = [] // how long idle each rule the context is over asks for
  if (t && tokens >= t.idle_at) due.push(IDLE_MS)
  if (t && tokens >= t.long_idle_at) due.push(t.long_idle_min * MINUTE_MS)
  if (due.some(d => idleMs >= d)) return 'compact'
  if (clear) due.push(idleMs + clear.opensIn) // the next window opening
  return due.length ? { at: Math.min(...due) } : null
}

async function compact($, st, tokens) {
  st.tried = true
  try {
    const r = await $.session.compact()
    $.ui.log(r.skip ? `hansa: idle compact skipped: ${r.skip}` :
      `hansa: compacted while idle: ${r.tokensBefore ?? tokens} -> ${r.tokensAfter ?? '?'} tokens`)
  } catch (err) {
    // refused while a turn runs: one has started since, so this one is given up, not an error
    $.ui.log(`hansa: idle compact not run: ${said(err)}`)
  }
}

// The clear, as if the person typed /clear, with no argument: once in this window. One that fails is not tried again
// until another turn starts
async function clear($, st, opened) {
  const was = st.cleared
  st.cleared = opened
  st.clearing = true
  try {
    await $.command.run({ command: 'clear' })
    st.since = await $.clock.now() // a fresh context, idle from now
    $.ui.log('hansa: cleared the session (the daily clear)')
  } catch (err) {
    st.cleared = was
    st.clearFailed = true
    $.ui.log(`hansa: daily clear not run: ${said(err)}`)
  } finally {
    st.clearing = false
  }
}

// When a turn ends and when a wait ends: decide, from the context, the settings file and the window as they are now.
// No compact once one was asked for, skipped or refused, and no clear once one failed, until another turn starts
// (whatever comes in between: a Stop)
async function check($, st) {
  cancel(st)
  if (!idle(st) || st.clearing) return
  const tried = st.tried
  try {
    const win = st.clearFailed ? null : window(await $.env.get('HANSA_MOD_CLEAR_DAILY'))
    if (tried && !win) return
    const t = tried ? null : await thresholds($)
    const { context } = await $.session.usage()
    const now = await $.clock.now()
    if (!idle(st) || st.clearing || st.tried !== tried) return // a turn started, or another check acted, meanwhile
    const tokens = context.tokens ?? 0
    const plan = daily(win, now, st)
    const d = decide(tokens, now - st.since, t, plan)
    cancel(st) // another check may have set one meanwhile
    if (d === 'clear') await clear($, st, plan.opened)
    else if (d === 'compact') await compact($, st, tokens)
    else if (d) st.pending = $.clock.after(d.at - (now - st.since), () => check($, st))
    if (d === 'clear' || d === 'compact') await check($, st) // what comes after it: the next window, compaction as above
  } catch (err) {
    $.ui.log(`hansa: idle check failed: ${said(err)}`)
  }
}

// The turn's end and its Stop may come in either order: the session is idle from the later of them
async function ended($, st) {
  if (idle(st) && st.since === null) st.since = await $.clock.now()
  await check($, st)
}

export function register(on) {
  const st = fresh()

  on('session.start', async ($, e, next) => {
    await born($, st)
    return next(e)
  })

  // A new turn (a message, hansa's post, a background task's report, ...): no compact until it ends
  on('turn.start', async ($, e, next) => {
    await born($, st) // a reload of the mod that came with no session.start: made now
    st.busy = true
    st.answered = false
    st.tried = false
    st.clearFailed = false
    st.stop = null
    st.since = null
    cancel(st)
    return next(e)
  })

  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    if (e.agent_id) return result
    st.stop = { background: (e.background_tasks || []).length, crons: (e.session_crons || []).length }
    if (st.stop.background || st.stop.crons) { // the session waits for work of its own: not idle
      st.since = null
      cancel(st)
    } else await ended($, st)
    return result
  }).catch(() => undefined) // never decides a Stop: failed, it is left out

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId) return result // a subagent's turn: only the main conversation counts
    st.busy = false
    st.answered = e.reason === 'answer' // interrupted, refused or failed: no compact
    await ended($, st)
    return result
  })
}
