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
// hansa gives it, in the environment, HANSA_MOD_SETTINGS: hansa's small JSON file, {"idle_at": tokens,
// "long_idle_at": tokens, "long_idle_min": minutes, ...}, read at each decision, so a change on hansa's page holds
// from the next one. It writes no file of its own.

const IDLE_MS = 60_000 // shorter than the prompt cache's lifetime
const MINUTE_MS = 60_000

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

// The thresholds in hansa's file as it is now: { idle_at, long_idle_at, long_idle_min }, each a positive whole
// number, or null when it cannot be had (that rule then never compacts)
async function thresholds($) {
  let x = null
  try {
    const path = await $.env.get('HANSA_MOD_SETTINGS')
    x = path ? JSON.parse(await $.fs.read(path)) : null
  } catch {
    x = null
  }
  const n = k => (x && Number.isInteger(x[k]) && x[k] > 0 ? x[k] : null)
  return { idle_at: n('idle_at'), long_idle_at: n('long_idle_at'), long_idle_min: n('long_idle_min') }
}

// The one decision, from the context's tokens, how long the session has been idle (ms) and the thresholds:
// 'compact' now; { at } to decide again once it has been idle that long (ms); or null, nothing
function decide(tokens, idleMs, t) {
  const due = [] // how long idle each rule the context is over asks for
  if (t.idle_at !== null && tokens >= t.idle_at) due.push(IDLE_MS)
  if (t.long_idle_at !== null && t.long_idle_min !== null && tokens >= t.long_idle_at) {
    due.push(t.long_idle_min * MINUTE_MS)
  }
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

// When a turn ends and when a wait ends: decide, from the context and hansa's file as they are now. Never once a
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
