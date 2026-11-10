// hansa's mod for the coordinator's session. `hansa coordinator` loads a copy of this folder with --plugin-dir,
// for that session only; workers never load it.
//
// Compaction when idle: once a turn of the main conversation has ended with an answer and the Stop that ended it
// listed no background tasks and no session crons, the session is idle. Then it compacts after IDLE_MS with no new
// turn when the context is at or above hansa's idle_at, while the whole context is still in the prompt cache. A new
// turn cancels any wait. While the session works it never interrupts: Claude Code's own auto-compact, at the window
// hansa gives it with --autocompact, does that.
//
// hansa gives it, in the environment, HANSA_MOD_SETTINGS: the desk's own settings file, the one hansa's page saves.
// At each decision it reads there the one number it uses under "coordinator_compact" (idle_at, in tokens), so a
// change on the page holds from the next decision; absent, or out of the page's range, it is its default. It uses
// only that number: nothing else of the file is kept, logged or repeated. It writes no file of its own, and keeps
// nothing in $.store.
//
// The daily clear: with `hansa coordinator --clear-daily HH-HH`, hansa also gives it HANSA_MOD_CLEAR_DAILY, a window of
// hours in local time (22-03 runs past midnight), for that start only. Once in each window it clears the session
// (/clear, as if typed) when the main conversation has been idle for CLEAR_IDLE_MS: hansa's post and anyone's message
// start a turn, so that is also that long with no post. Not in a window this state was made in: a session started then
// has just been told what to do, and a reload of the mod, which forgets what it knew, does not clear twice. Inside a
// window it is still to clear in, idle compaction waits for the clear; outside one, or once cleared, it is as above.
// hansa's SessionStart hook for clear then prints hansa status into the fresh context, and records the clear.
//
// The status line: when hansa gives it HANSA_MOD_SUMMARY (whenever it loads the mod: plugin.py MOD_NEEDS), it pins one
// line under the prompt with $.ui.status, for the person at the terminal (the model never reads it), beside a status
// line of the person's own, which it leaves as it is. It reads the summary hansa serve writes at each round (plugin.py:
// summary) every LINE_MS and when a turn of the main conversation ends: hansa and the desk's name; the session's model
// as /model shows it, and the effort of the latest model request (before the first, the one the start gave,
// HANSA_MOD_EFFORT); then what is wrong, what waits for the principal, the work and the last 24 h's cost; last, the
// page's address, never its key. A summary missing or older than STALE_MS: hansa serve is not running. Words in the
// alert language (the summary's; HANSA_MOD_LANGUAGE until there is one). With HANSA_MOD_LINE_ONLY=1 (a session hansa
// up starts through hansa notify-launch) the line is all it does: no idle compaction and no daily clear.

const IDLE_MS = 60_000 // shorter than the prompt cache's lifetime
const MINUTE_MS = 60_000
const CLEAR_IDLE_MS = 5 * MINUTE_MS
const LINE_MS = 15_000 // how often the line reads the summary again
const STALE_MS = 2 * MINUTE_MS // a summary older than this: hansa serve is not running (or its rounds are stuck)
// The threshold's default and range, as hansa's (desk.py: COMPACT_DEFAULTS, COMPACT_MOST)
const THRESHOLDS = {
  idle_at: [400_000, 100_000, 1_000_000],
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
    lined: false, // the line's refresh was set going (or there is no line)
    effort: undefined, // the effort of the main loop's latest model request ('' for none); undefined before one
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

// The threshold in the desk's settings file as it is now: { idle_at }, or null when the file cannot be read or
// parsed (then nothing compacts while idle)
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

// The one decision, from the context's tokens, how long the session has been idle (ms), the threshold (null: no
// compact) and the daily clear (null: none): 'clear' or 'compact' now; { at } to decide again once it has been idle
// that long (ms); or null, nothing. Inside a window still to clear in, only the clear: compaction waits for it
function decide(tokens, idleMs, t, clear) {
  if (clear && clear.endsIn !== undefined) {
    if (idleMs >= CLEAR_IDLE_MS) return 'clear'
    return { at: Math.min(CLEAR_IDLE_MS, idleMs + clear.endsIn) }
  }
  const due = [] // how long idle the idle rule asks for, when the context is over its threshold
  if (t && tokens >= t.idle_at) due.push(IDLE_MS)
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
// (whatever comes in between: a Stop). While a clear has not returned none starts, and one under way reads nothing more
async function check($, st) {
  cancel(st)
  if (!idle(st) || st.clearing) return
  const tried = st.tried
  const moved = () => !idle(st) || st.clearing || st.tried !== tried // a turn started, or another check acted, meanwhile
  try {
    if (await $.env.get('HANSA_MOD_LINE_ONLY') === '1') return // the launcher's session: the line alone
    const win = st.clearFailed ? null : window(await $.env.get('HANSA_MOD_CLEAR_DAILY'))
    if (tried && !win) return
    if (moved()) return // not even a read of the settings
    const t = tried ? null : await thresholds($)
    const { context } = await $.session.usage()
    const now = await $.clock.now()
    if (moved()) return
    if (st.made === null) st.made = now // made before anything said when: as if now, so not in this window
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

// The turn's end and its Stop may come in either order: the session is idle from the later of them. Claude Code
// refuses a compact or a clear under the Stop hook (it would wait on the turn the hook holds): after a Stop, the
// decision comes once the hook has returned
async function ended($, st, stop = false) {
  if (idle(st) && st.since === null) st.since = await $.clock.now()
  if (!stop) return check($, st)
  cancel(st)
  st.pending = $.clock.after(0, () => check($, st))
}

// -- the status line -------------------------------------------------------------------------------------------------

const WORDS = {
  en: {
    none: 'hansa serve not running (it has written nothing yet)',
    stale: m => `hansa serve not running (no update for ${m} min)`,
    paused: 'dispatch paused', disk: 'disk under its floor', memory: 'memory under its floor',
    api: at => `API errors since ${at}`, push: s => `push failed: ${s}`, noChannel: 'no alert channel',
    waiting: parts => `waiting for you: ${parts.join(', ')}`, releases: n => `releases ${n}`, todos: n => `to-dos ${n}`,
    idle: 'idle', running: n => `running ${n}`, queued: n => `queued ${n}`, review: n => `review ${n}`,
    failed: n => `failed ${n} in 24 h`, cost: d => `24 h $${d}`, page: a => `page ${a}`,
  },
  zh: {
    none: '后台没在跑（还没有记录）',
    stale: m => `后台没在跑（${m} 分钟没更新）`,
    paused: '派发已暂停', disk: '磁盘空间低于下限', memory: '内存低于下限',
    api: at => `API 出错（自 ${at}）`, push: s => `推送失败：${s}`, noChannel: '没有提醒渠道',
    waiting: parts => `等你：${parts.join('、')}`, releases: n => `发布 ${n}`, todos: n => `待办 ${n}`,
    idle: '空闲', running: n => `进行 ${n}`, queued: n => `排队 ${n}`, review: n => `待审 ${n}`,
    failed: n => `24 小时失败 ${n}`, cost: d => `24 小时 $${d}`, page: a => `网页 ${a}`,
  },
}

// Text from a file or the engine, as one short line: no control characters
function plain(x, most = 80) {
  return String(x ?? '').replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, most)
}

const count = x => Number.isInteger(x) && x > 0 ? x : 0

// The summary as the line uses it, each field of its kind; null when it is not one (no time it was written)
function summaryOf(x) {
  if (!x || typeof x !== 'object' || Array.isArray(x) || !Number.isFinite(x.at)) return null
  const page = typeof x.page === 'string' && /^https?:\/\/[^\s?#]+$/.test(x.page) ? x.page : '' // never a key
  return {
    at: x.at * 1000, desk: plain(x.desk), language: x.language === 'zh' || x.language === 'en' ? x.language : '',
    page: plain(page, 200), paused: x.paused === true, disk: x.disk_low === true, memory: x.memory_low === true,
    api: Number.isFinite(x.api_errors_since) && x.api_errors_since > 0 ? x.api_errors_since * 1000 : null,
    push: Array.isArray(x.push_failed) // a service's name, never more: an address may hold its key
      ? x.push_failed.filter(s => typeof s === 'string' && /^[A-Za-z0-9_-]{1,20}$/.test(s)).slice(0, 5) : [],
    noChannel: x.no_channel === true, releases: count(x.releases), todos: count(x.todos), running: count(x.running),
    queued: count(x.queued), review: count(x.review), failed: count(x.failed_24h),
    usd: Number.isFinite(x.usd_24h) && x.usd_24h > 0 ? Math.round(x.usd_24h) : 0,
  }
}

const hhmm = ms => {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// The line, from the summary (null: none, or not one), the time now (ms), the model and effort, and what the start
// handed over: { desk, language }
function lineText(sum, now, model, effort, given) {
  const w = WORDS[(sum && sum.language) || (given.language === 'zh' ? 'zh' : 'en')]
  const head = [`hansa ${(sum && sum.desk) || plain(given.desk) || '?'}`]
  const who = [plain(model, 60), plain(effort, 20)].filter(Boolean).join(' ')
  if (who) head.push(who)
  if (!sum) return [...head, w.none].join(' · ')
  const age = now - sum.at
  if (age > STALE_MS) return [...head, w.stale(Math.floor(age / MINUTE_MS))].join(' · ')
  const out = [...head]
  if (sum.paused) out.push(w.paused)
  if (sum.disk) out.push(w.disk)
  if (sum.memory) out.push(w.memory)
  if (sum.api !== null) out.push(w.api(hhmm(sum.api)))
  if (sum.push.length) out.push(w.push(sum.push.join(', ')))
  if (sum.noChannel) out.push(w.noChannel)
  const waits = [sum.releases && w.releases(sum.releases), sum.todos && w.todos(sum.todos)].filter(Boolean)
  if (waits.length) out.push(w.waiting(waits))
  if (!sum.running && !sum.queued) out.push(w.idle)
  for (const k of ['running', 'queued', 'review', 'failed']) if (sum[k]) out.push(w[k](sum[k]))
  out.push(w.cost(sum.usd))
  if (sum.page) out.push(w.page(sum.page))
  return out.join(' · ')
}

// Read the summary and pin the line again. Never fails: what went wrong leaves the line as it was
async function refresh($, st) {
  try {
    const path = await $.env.get('HANSA_MOD_SUMMARY')
    if (!path) return // no line: hansa gave none (a start that did not hand one over)
    let sum = null
    try {
      sum = summaryOf(JSON.parse(await $.fs.read(path)))
    } catch {
      sum = null // missing or unreadable: as if hansa serve never wrote it; what the read said is not repeated
    }
    let model = ''
    try {
      model = await $.session.model()
    } catch {
      model = ''
    }
    const effort = st.effort !== undefined ? st.effort : await $.env.get('HANSA_MOD_EFFORT')
    const given = { desk: await $.env.get('HANSA_MOD_DESK'), language: await $.env.get('HANSA_MOD_LANGUAGE') }
    $.ui.status(lineText(sum, await $.clock.now(), model, effort, given))
  } catch {
    // the line stays as it was
  }
}

// Once per loaded copy of the mod (a session's start, or the first hook after a reload): the line now, and every
// LINE_MS after
async function lineStart($, st) {
  if (st.lined) return
  st.lined = true
  try {
    if (!(await $.env.get('HANSA_MOD_SUMMARY'))) return
    $.clock.every(LINE_MS, () => refresh($, st))
    await refresh($, st)
  } catch {
    // no line
  }
}

export function register(on) {
  const st = fresh()

  on('session.start', async ($, e, next) => {
    await born($, st)
    const result = await next(e)
    await lineStart($, st)
    return result
  })

  // The effort of the main loop's latest model request, as it goes out: observed, passed on unchanged
  on('turn.step', async function* ($, e, next) {
    if (!e.agentId) st.effort = e.effort === undefined ? '' : String(e.effort)
    return yield* next(e)
  })

  // A new turn (a message, hansa's post, a background task's report, ...): no compact until it ends
  on('turn.start', async ($, e, next) => {
    await born($, st) // a reload of the mod that came with no session.start: made now
    await lineStart($, st)
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
    if (e.effort && typeof e.effort.level === 'string') st.effort = e.effort.level // the turn's, as hooks give it
    st.stop = { background: (e.background_tasks || []).length, crons: (e.session_crons || []).length }
    if (st.stop.background || st.stop.crons) { // the session waits for work of its own: not idle
      st.since = null
      cancel(st)
    } else await ended($, st, true)
    return result
  }).catch(() => undefined) // never decides a Stop: failed, it is left out

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId) return result // a subagent's turn: only the main conversation counts
    st.busy = false
    st.answered = e.reason === 'answer' // interrupted, refused or failed: no compact
    await ended($, st)
    await lineStart($, st)
    await refresh($, st) // the line, as the turn ends
    return result
  })
}
