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
// no file of its own.

const IDLE_MS = 60_000 // shorter than the prompt cache's lifetime
const MINUTE_MS = 60_000
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
  }
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

// The one decision, from the context's tokens, how long the session has been idle (ms) and the thresholds:
// 'compact' now; { at } to decide again once it has been idle that long (ms); or null, nothing
function decide(tokens, idleMs, t) {
  if (!t) return null
  const due = [] // how long idle each rule the context is over asks for
  if (tokens >= t.idle_at) due.push(IDLE_MS)
  if (tokens >= t.long_idle_at) due.push(t.long_idle_min * MINUTE_MS)
  if (due.some(d => idleMs >= d)) return 'compact'
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

// When a turn ends and when a wait ends: decide, from the context and the settings file as they are now. Never once a
// compact was asked for, skipped or refused, until another turn starts (whatever comes in between: a Stop)
async function check($, st) {
  cancel(st)
  if (!idle(st) || st.tried) return
  try {
    const t = await thresholds($)
    const { context } = await $.session.usage()
    const now = await $.clock.now()
    if (!idle(st) || st.tried) return // a turn started meanwhile
    const tokens = context.tokens ?? 0
    const d = decide(tokens, now - st.since, t)
    cancel(st) // another check may have set one meanwhile
    if (d === 'compact') await compact($, st, tokens)
    else if (d) st.pending = $.clock.after(d.at - (now - st.since), () => check($, st))
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

  // A new turn (a message, hansa's post, a background task's report, ...): no compact until it ends
  on('turn.start', async ($, e, next) => {
    st.busy = true
    st.answered = false
    st.tried = false
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
