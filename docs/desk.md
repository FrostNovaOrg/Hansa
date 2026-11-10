# The desk: files, keys and notes

This reference covers the desk's [web page](#status-work-and-cost-on-the-web-page),
[settings](#settings-on-the-web-page), [files](#a-desks-files),
[backups and moves](#backing-up-and-moving-a-desk), and
[notifications to the coordinator](#hearing-from-hansa).

Without `--desk` or `HANSA_DESK`, hansa searches the current folder and then its
parents for `settings.json` and a `tasks/` directory. If either cannot be
looked at (for example, a run's sandbox closes an enclosing desk), it skips
that folder and keeps searching. With no desk found, commands that need one
give the usual "no desk here" refusal.

## Creating a desk and lanes

For `hansa init` and startup, follow
[Getting started](getting-started.md#create-and-start-the-desk).
With `--repo PATH`, `hansa init` and `hansa lane add` without `--like` make a
repository and an empty `Initial commit` when PATH is new or empty (a `.DS_Store`
is ignored). The project folder and each missing parent folder are made with
mode 0700, including under umask 0002; existing folders keep their modes.
If the nearest existing folder is inside a git checkout, the command refuses
without making anything, to avoid creating a nested repository by mistake.
An existing repository with no
commit gets the same first commit only when it holds just `.git` and has nothing
staged. Hansa never chooses files to put into git: a project with files must be
committed first; the refusal gives commands with its path filled in.
The commit uses the [lane's identity](who-changes-what.md#commit-messages-and-identity),
including `lane add --identity`, else the repository's own config, else hansa's.
The command prints the repository's path and branch. On Linux, `init --user`
creates it as that account.

For these new or empty repositories, `--base` must be a valid branch name and
becomes the first commit's branch. Without it, git init chooses the branch, or
an existing empty repository keeps the branch HEAD names. Invalid bases and
other refusals are checked before any folders, repositories or desk files are
made. If creation itself fails, hansa stops and says what it made and what it
did not make; any partial repository or folders remain for you to inspect.

For a checkout that already has a commit, an explicit `--base` with either
command must resolve to a commit; this also applies to `hansa lane add L --like OTHER
--base BRANCH`. A missing base or a reference to a tree or file is refused
before desk files, lane settings or standing instructions are written, so a
base typo is caught before the first task. The refusal says how to list the
checkout's branches and retry. Existing tags and commit IDs remain accepted.
Without `--base`, the checkout's current branch is used, or `--like` copies
the other lane's base.

`hansa init` makes a new desk folder and its `tasks/`, `lanes/` (including
the initial lane), `.hansa/`, `notes/`, `secrets/` and `.claude/` folders
with mode 0700, including when the account's umask is 0002. New enclosing
folders are private too. With `--user`, root makes the desk folder 0700
before giving it to the account; enclosing folders root makes retain their
usual modes so the account can traverse them.
Existing folders keep their modes. In an existing group- or world-writable
desk folder, init still makes the desk and warns:
`warning: others may write in <folder>; run chmod go-w <folder>`.
The command quotes a path containing spaces or shell characters.

Settings and JSON state files use UTF-8. Filename bytes that are not UTF-8,
as Linux permits, are stored as JSON escapes and restored when the file is
read. Ordinary Unicode text stays readable, and saved command lines keep
their original spelling.

## Status, work and cost on the web page

For opening the page and setting providers, models and prices, follow
[Getting started](getting-started.md#open-the-principals-page); for a remote
desk, use its [SSH tunnel instructions](getting-started.md#work-over-ssh).
The page keeps
its token in `sessionStorage`, scoped to the browser tab and origin (scheme,
host and port), and removes it from the address before requesting desk data.
Reloading in that tab keeps access; another desk or forwarded port has its own
storage. If session storage is unavailable, access lasts for the current page
load; reopen the printed address after a refresh. The token is never stored in
`localStorage` or issued as a cookie, and responses use `Referrer-Policy: no-referrer`.
The HTML shell can load without a token; every API request must send
`Authorization: Bearer <token>`, and POST also requires `X-Hansa: 1`.
Query parameters and cookies do not authenticate API requests.

After upgrading, restart the desk's server and reopen the printed address:
the token is renewed automatically when its format changes. A current token
persists across server restarts. Closing a tab discards its session storage
unless the browser restores that session. To revoke all copies, stop the
desk's server, delete `.hansa/web-token`, restart it and open the new address.
No browser, proxy or server setting is needed for this authentication.

The API returns HTTP 400 with JSON fields `ok: false`, `code`, `field` and `problems` for an
invalid `days` query value (`invalid_query`, field `days`), malformed JSON
(`invalid_json`, field `body`), or a JSON body whose top level is not an object
(`invalid_type`, field `body`). `problems` is a list of explanations. Work and cost
defaults to 7 days, clamps integer values to 1–90 and accepts `all`; invalid event counts
continue to use the default of 60. An empty POST body still means `{}`.

The browser tab and the caption beside Hansa use the
[desk name saved by the principal](#settings-on-the-web-page). With no name set,
they use its parent folder and its own folder, such as `work/desk`.
The tab title is `Hansa · NAME`,
prefixed with the number of releases waiting for approval, open to-dos and
providers resting until a top-up is confirmed when that count is nonzero.
It updates with each status refresh.

Up to 860 px wide the tabs wrap onto a second row, so every tab stays in view
without a sideways scroll; wider windows keep them in one row. Under 640 px a
page's heading takes one column: on Status, the “Auto updates” note comes below
the tagline.

Goals lists the open goals recorded with `hansa goal add`. Each row uses the counts
from `hansa goal list`: queued/running, waiting for review, merged, failed, open
to-dos, merged chains missing evidence and released chains, plus its lifetime
cost. “Show done and dropped goals” includes ended goals with their outcome.
With no goals, the empty state explains how to record one.
Goal rows and totals use the same “Token usage”, “Estimated value” and “Unpriced
tokens” labels as Work and cost, in either language, and the same two-decimal
`US$` format (for example, `US$0.012000` is shown as `US$0.01`).

Open a goal to read the principal's reason, current decisions with their reasons,
author, time and task, and replaced decisions in a closed fold. The work section
uses `hansa goal show`: the latest task in each retry chain, state, rounds, check,
verification, merge and releases, plus open to-dos, total cost and rounds.
The English release label is “Release” for one release and “Releases” for more.
Nonempty `missing:` lines are highlighted and keep the command's exact wording;
an empty missing list says “nothing”. The labels are available in English and
Chinese; recorded text stays as recorded and displays as plain text. Task numbers
open the existing task details. Tables scroll within the page at phone widths.
The Chinese labels use the same review, to-do and token price terms as the other tabs.
See [Goals and product decisions](tasks.md#goals-and-product-decisions) for what
the counts and evidence mean.

The read-only `/api/goals` route requires the usual bearer token. It returns an
array combining each `goal list --all --json` record (including `counts`) with
its `goal show G --json` record, including cost. It shares one full events-file
read across the goals, supports gzip and returns 304 for an unchanged ETag.
The tab fetches when opened and once a minute while it and the document are
visible. Failed requests retain the last report with a reconnection notice and
retry button; consecutive failures increase the interval up to ten minutes.
Returning to the tab or document requests fresh data immediately. Requests do
not overlap; the next interval starts after the request finishes. Expanded
decisions stay open across updates, and a 304 leaves the displayed details in
place. This tab only reads records; changes are recorded through the commands.

All tasks lists every work task, newest queued first, 50 per page through to the
oldest. Each line shows its number, title, lane, current state, queued time,
ended time and latest verification verdict. These come from
`tasks/<id>/meta.json`: `id`, `title`, `lane`, `state`, `created`,
`finished`, and the `verdict` in the task named by `verify`. Ended is the saved
worker finish time, not the merge time. Missing fields stay blank. Original tasks
and their follow-ups both appear; an original's state remains Replaced.
Verification tasks have no rows of their own. Click a line or its number to open
the same task details used by Status: text, report, changes, check, verification
links and steps. Titles and reports are displayed as plain text.

State, lane, title words and queued-time filters combine. Title words are matched
without case, and every word must occur in the title. Queue periods are the same
rolling 1, 7, 30 and 90 days or all history as Work and cost; all history is the
default. A bounded period leaves out tasks without a recorded queue time. Lane
choices include lanes in old task records. Changing filters starts at page one;
Previous page and Next page move toward newer and older tasks. Returning to the
tab refreshes its records while keeping its filters for this page load.
`/api/tasks` accepts `page` (starting at 1), `state`, `lane`, `words` and `days`
(`1`, `7`, `30`, `90` or `all`), with the usual page key. It returns `rows`,
`page`, `pages`, `total`, `states` and `lanes`; out-of-range pages use the nearest
available page. Invalid page numbers or periods return HTTP 400. It reads task
metadata once per request, without git history, reports or logs.

Known gaps lists the verification notes kept by `hansa gaps`, oldest first,
including notes from closed tasks. Each row shows the note ID, its task's title
(click to open that task's details), the whole note, the latest mark (none yet,
later, wontfix or fixed), its reason, who marked it and when. The mark filter
defaults to unmarked notes and those marked later; All includes every note,
and each mark can also be selected alone. An empty selection says “No known gaps”.
This view is read-only: use `hansa gaps mark ID fixed|later|wontfix "why"`
to mark a note. Returning to the tab refreshes its records and keeps the filter
for this page load. Text and reasons display as plain text, with the keys hansa
knows removed. `/api/gaps` reads the same saved notes as the command and requires
the usual page key.

Status refreshes every five seconds while Status is visible. While an input,
textarea or select has focus, background refreshes update the header but leave
the tab's fields in place. With focus on a button or link, the lists keep
updating and focus returns to the same action on the same record, if it is
still available. Expanded task details stay open. Submitting an action refreshes
the tab immediately. Unsent release approval or stop reasons survive these
redraws in this page's memory. Each draft stays with its release and action,
and is dropped when that action is no longer available. Reloading or closing
the page discards the drafts.

In the provider table, a provider without a recorded Ping says “not checked yet”
in a neutral colour. Green “ready” requires a successful recorded check and no
current blocking problem. A failed last check has a warning; a current problem
(such as low credit or a rest) takes precedence. The last check's result, detail
and time remain visible. These labels do not change which providers can be
scheduled; an unchecked provider can still be tried.

The header shows the saved desk name under Hansa at every width, or its last
two path folders, such as `shop/desk`, when no name is set.
Long names are cut with an ellipsis; hovering shows the whole name.
The summary stays one line high, on its own row in narrow windows.
It cycles through desk, dispatch, server, version, install time, current time
and status notes, showing warnings first and keeping each group's order.
Short items change about every four seconds. A long item rests briefly, moves
from its start to its end at reading pace, and rests again before the next item;
a lone item repeats that movement. With reduced motion, long items are cut with
an ellipsis instead, while items still change. Refreshes keep the current item
and its progress, updating its text; a removed item gives way to the next.
The dot keeps the summary's overall status colour. The list button at the end
opens all items in full, with warnings marked, over the page below the header.
It stays open and updates on refresh. The button again, a click elsewhere, or
Escape closes it. Both header buttons are reachable with Tab; Escape returns
focus to the button that opened the list or menu. On the Chinese page, the desk
and version items are labelled
`工作台` and `hansa 版本`; the English current-time item uses `now`, as `hansa status` does.

When releases await approval, to-dos are open or providers need a credit top-up,
Status puts them first, before the pipeline and summary cards. Each to-do shows
its number, what the principal must do, why only they can do it (if given),
whether work waits, and when it was recorded. To-dos are
read-only: the principal answers in the conversation and the coordinator records
the outcome and closes them.
The credit group is labelled **Top-ups to confirm**
(**等你确认充值** in Chinese). Each provider waiting for credit shows its name
and when the rest began, with the same **Topped up** button as Settings, Providers
directly underneath.
Confirming a top-up removes that waiting item and updates the count. Usage-limit
rests end by themselves and do not appear among the principal's waiting items.

Release controls remain beside their number and lane/target, with the command
below. Only lists longer than five lines, such as commits, files and a to-do's
remaining lines, fold and open on a click. Notes and warnings stay visible.
Open folds stay open across status refreshes until closed or their item stops
waiting. Folded release lists keep their styling and scrolling. Reloading or
closing the page discards the fold state. Controls wrap in narrow windows.
Recent releases stay further down. With nothing waiting, Status keeps its usual order.

The To-dos section lists every item, open and closed, newest first, 50 per page,
with an all/open/closed filter. It includes the outcome and closure time. Nothing
is trimmed. Requests recorded by older versions appear here too, and open ones
can be closed with the same command. Old page answers remain visible as outcomes.
The page has no endpoint for recording, answering or closing to-dos;
`GET /api/todos?page=1&state=open` reads them with the usual bearer key.
Omit `state` for all items or use `closed`; page numbers are clamped to the
available pages. Text, reasons and outcomes display as plain text.

Status otherwise starts with a pipeline strip: queued tasks, running workers, running checks,
awaiting review, running verifications and merge checks. Queued counts the queued
list; workers and checks split the running tasks by their current step, excluding
verifications. Awaiting review counts review tasks with no verification running
(including those whose verification is queued); running verifications counts their
running verifier tasks, including a verifier's check. Each task appears in one box.
Merge checks include those queued. A separate box counts waiting releases
plus open to-dos. Clicking a box scrolls to its list (running stages use the lane
tracks). Zero counts are dimmed.

Below the strip, lane tracks replace the Lanes and Running tables, with running
workers, checks, verifications, merge checks and lane tries first, then dimmed lanes,
preserving status order within each group. Each retains the providers in order,
their models, the active provider and model, task link and title, browser marker and check timing. The
merge and verify lanes also name the lanes they serve and the work they accept.
The marked step distinguishes workers, checks, verifications (with their parent task)
and merge checks. A running merge check still appears on its occupied lane if that
lane is unmarked as a merge lane. The elapsed bar counts from the same start as
the server's timeout: the task's start through its first check, and the check's
start for a retried check (`check_retry` in `/api/status`). The row's check text
still shows the check's own minutes and the task's total minutes. The bar uses
the running item's `limit_min` from `/api/status`: tasks and verifications
use their lane's `timeout_min` (240 by default); merge checks use the occupied
lane's `merge_timeout_min`, falling back to its `timeout_min` and then 240.
The bar warns past three quarters. The line below
shows the estimated start time, replies, idle minutes, last note and lane queue
count; unavailable progress reads `—`. Free lanes with no queue show their
name, providers and models, and free pill, without a step track; a free merge or
verify lane also shows its role sentence in the same line. Free and queued lanes
are dimmed, tries are named, and sandbox holds have a held pill and show their reasons.
Warning pills, including sandbox holds and queued counts, use a warning triangle.
When `hansa serve` is stopped,
the picture still shows the desk's records; the page's status line warns once
that new tasks will not start. When fetching `/api/status` fails, both parts show
an error in place of their previous figures. An events fetch failure appears in
Recent events and leaves the status picture visible.
At widths up to 640 px the strip's stages and lane steps take two rows; labels
wrap between words, the principal box follows below the stages, and tracks fit
the page without horizontal scrolling.
The picture has no animation and uses the page's English and Chinese dictionaries.

Work and cost shows a rolling 24-hour timeline below the totals and their
explanation, above the breakdown tables. Its window stays at 24 hours for every
selected cost period. It has one row per current lane
(including empty lanes), with hourly marks from 24 hours ago to now. Removed
lanes are omitted. Workers, lane checks, verifications, merge checks, lane tries
and releases each have a palette colour; failures are red. Running work extends
to now with a fading, dashed end. Work crossing the left edge is clipped there.
Hover, focus or tap a segment for its kind, task IDs and titles (or release number
and target), original start, minutes and result. Clicking a task segment opens
the same task details as its ID elsewhere; a merge batch names all its tasks.
Concurrent pieces stack within their lane's row so each remains selectable.
Checks that could not run say so. The legend and details use the page language.
The axis fits the page on phones, with fewer hour labels but all hourly marks;
labels have an opaque background so adjacent grid lines do not cross their text.
History loads when Work and cost opens, at most once a minute, independently of
the cost report. Read failures appear in the timeline section, keeping the last
successful timeline when available.

`GET /api/timeline` uses the same bearer authorization as the other page endpoints
and only reads records. Its top-level `start` and `end` are Unix seconds bounding
the window. `lanes` is an ordered list of `{lane, segments}`. Each segment has:

- `kind`: `worker`, `check`, `verification`, `merge`, `try` or `release`;
- `lane`: the lane that executed it, including a dedicated merge lane;
- `started`, `ended`: original Unix times (`ended: null` for running work);
  an unknown older start uses the window's left edge;
- `start`, `end`: drawing times clipped to the window;
- `minutes`: elapsed minutes from the original start, to two decimal places;
- `running`, `failed`, `could_not_run`: flags for presentation;
- `result`: the recorded state/verdict, or `running`, `done`, `passed`, `failed`,
  `stopped`, `queued`, `cancelled` or `could_not_run`;
- `tasks`: `{id, title}` entries, including every member of a merge batch;
- `release`: `{id, target}` for a release, otherwise `null`.

The view is `hansa.views.timeline(desk)`; `timeline_data(lanes, tasks, events,
releases, tries, at)` builds the same data from records without a web server.
Worker and verification boundaries come from `task_started` / `task_finished`;
`started` / `finished` and earlier attempts' `ran_from` / `ended` supply record
boundaries. A verification uses its task's verdict. Lane checks pair
`check_started` with `task_checked` (or `check_killed` before a retry); records
retain `check_started` and the check's `seconds`. A refused check without a
start is a labelled point at `task_checked`. Merge checks pair
`merge_check_started` with `merge_checked`, `merge_check_ended` or
`merge_check_killed`; `members` makes a batch one segment. Their records retain
`merge_check.started` or a result's `at` and `seconds`, and the execution `lane`.
Lane tries pair `lane_try_started` / `lane_tried`, supplemented by the lane's
try record's `started` / `ended`. Releases pair `release_started` with
`release_ended` or `release_stopped`, supplemented by their `started` / `ended`.
Records supply running and crossing work and ends saved before a finish event
could be appended. Event pairs preserve earlier runs and retries; an end
following another end does not duplicate a merge check.

The endpoint reads the event log backwards in 64 KiB blocks to the start of the
window, skipping damaged lines. An end explicitly saying that work never
started (a task cancelled before launch, a refused check or an unstarted release)
does not trigger an older-history search. A refused check remains a labelled
point at its end. For a crossing pair, the endpoint first takes the start from
the task, earlier run, release or lane try record. Only when no matching record
supplies it does it search older events, stopping seven days before the window.
If no start is found within that limit, the segment begins at the window's left
edge; its minutes then count from that edge. It keeps recovered boundaries in
memory until their ends leave the window, including earlier tries whose lane
record has since been replaced. A replaced or truncated log invalidates the
remembered boundaries. Thus neither a server restart nor a new unmatched end
causes a search through the desk's entire history. Existing status responses
and command output do not include timeline data.

Status also shows the last 24 hours in summary cards and puts pending release
decisions and to-dos first. Its Auto updates mark carries the note about
five-second updates and keeping controls in place while editing. Empty
states explain where new activity will appear. Task details remain open across
refreshes. Release approvals, refusals and stops keep their existing controls.

Dates and times follow the page's selected Chinese or English language, including
the lane's estimated start time. In a task box's steps row, times show hours and
minutes, with the month and day added for a different local calendar day; seconds
are omitted. Durations below 90 seconds use seconds; longer durations use minutes,
with one decimal below 10 minutes and whole minutes from 10 minutes onward.

Queued tasks show their waiting reason in the page's language, both in the queue
and under a lane's queued count. `/api/status` keeps the English `waiting` field
and adds `waiting_code` and `waiting_values`. The command views still use the
same English sentence. Codes and values are:

| Code | Values |
|---|---|
| `paused`, `no_lanes`, `no_work_lanes`, `slot` | none |
| `ready` | `lane`, `provider`, `server` (up, busy, gone or stuck), `seconds` since the last round |
| `disk` | `volumes`: each has `mount`, `free`, `floor`, `again` (the free space needed to resume) |
| `memory` | `available`, `floor`, `again` |
| `lanes` | `reasons`: ordered `{code, values, text}` objects, with each English clause in `text` |

Lane reasons use `missing_lane`, `merge_lane`, `verify_lane` or `busy` with
`lane`; `sandbox` has `lane`, `kind` and the sandbox's `why`; `providers` has
`lane` and ordered `providers` reason objects. Provider codes are `refused`,
`missing_provider`, `no_endpoint`, `no_key`, `credit`, `rest`, `unreachable` and
`unknown`. Their values have `provider`, `since`, `until`, `minutes`, `how`
(fixed, reset, credit or blank) and `why`; unused values can be null or blank.
Sizes include their units, and times are Unix seconds. Unknown codes, including
nested ones, keep the server's English sentence. Error details and names remain
as the server sent them.
Login and rejected-key rests also keep the server's recovery sentence in both
page languages, including the login command or how saving a new key restores it.

The status line also warns about `serve_code`, `alert_failed` and every line of
`unreached`, with short translated labels and the server's original text.

Recent events keeps the newest 20, with their times. The page reads the log from
the end in 64 KiB blocks, reading each byte at most once per request even when an
event spans many blocks. Unicode line boundaries are preserved; a damaged final
line is skipped. Requests filtered by event kind still search the whole log.
These kinds use a short sentence in the page's language and a task link when they
carry `task`:

- Tasks: `task_started`, `task_finished`, `task_merged`.
- Checks and verifications: `check_started`, `task_checked`, `check_killed`,
  `check_rerun_queued`, `verification_closed`.
- Merge checks: `merge_queued`, `merge_check_queued`, `merge_check_started`,
  `merge_checked`, `merge_check_ended`, `merge_check_killed`,
  `merge_check_cancel`, `merge_check_base_moved`, `merge_batch_split`.
- Releases: `release_asked`, `release_answered`, `release_started`,
  `release_ended`, `release_void`, `release_forbidden`, `release_waits`,
  `release_stopping`, `release_stopped`.
- Lane tries: `lane_try_queued`, `lane_try_started`, `lane_tried`,
  `lane_try_withdrawn`.
- Sandbox holds: `sandbox_held`, `sandbox_ready`.
- Provider rests and the server: `provider_credit`, `server_stopped`.

Other kinds retain their raw event name and `key=value` text. A missing check
result reads as unknown; a check that could not run says so.
`release_waits` names the lane and target and says the release needs the principal's
answer again because its target left the releases-without-asking list or no longer
matches what that list recorded.

Work and cost offers the last 1, 7, 30 or 90 days and **Since the desk began**,
with token usage, estimated value and unpriced tokens above the six category tables.

Above the period choice, the work calendar shows one square per local day from
the desk's start through today (including today's usage so far), capped at the
last 53 weeks. Weeks run left to right with Monday at the top; dates before the
desk began and after today stay blank. On phones the panel scrolls sideways,
opening at the newest week. Hovering a day shows its date, tokens and `US$`
value, using the same totals as **By day**; screen readers read these values
when a day has focus.

The scale top T is the greater of 10,000,000 tokens and the nearest-rank 90th
percentile of visible days with tokens (sorted position `ceil(0.9 × N)`). Empty
days use the empty shade; positive days use four shades, up to T/4, T/2, 3T/4,
and above 3T/4, including days above T. The legend names T and whether the
percentile or floor set it. The page requests `/api/cost/calendar` when the tab
opens, at most once a minute, independently of the selected period. This keyed
endpoint returns `start` and `today` as local date keys and `days` as the report's
`by.day` totals within those bounds; zero-usage dates may have no row. A calendar
error stays in its panel while the period report loads as usual. After a failed
request, reopening the tab or retrying the period report retries the calendar
immediately.

Money on the Status and Work and cost pages uses `US$` before every amount,
including table cells, chart labels and tooltips (for example, `US$305.31`).
Currency headings and notes say **USD** in English and **美元** in Chinese.
The price form keeps its USD-per-million-token units; downloaded CSV data and
values sent to the server are unchanged.
Alongside these totals it counts:

- Tasks finished and failed, once per task, from `tasks/<id>/meta.json`'s
  outcome and `finished` time; automatic retries are not extra tasks. Successful
  tasks count as finished even after merging, including while a merge is pending.
  A replaced task keeps its original `ended_as` outcome, so both it and its
  follow-up count independently. Verifications count separately.
- Tasks merged, from each task's `merged` time. Finishing and merging in different
  periods contributes to each period's respective count.
- US$ per merged task, from the period's estimated value (the same total the page
  shows) divided by the tasks merged in it (`usd_per_merged`); with no merge it
  shows "—".
- Merged at the first try: the share of the tasks merged in the period that were
  not follow-ups made by `hansa retry` (`merged_first_try` of `merged`), shown
  with its counts. A follow-up made by `hansa retry` counts as sent back, once,
  even when it is itself a retry of a retry. With no merge it shows "—".
- Approximate lines added and removed by those merged tasks, parsed from the
  saved `stat` (`git diff --shortstat` at task completion) in their metadata;
  the page never runs git to obtain these counts. Tasks without a readable size
  record are excluded and their number is shown. An insertion-only or
  deletion-only record contributes zero on the other side. Binary and rename-only
  records can contribute zero lines. A replaced original contributes no lines
  unless it was itself merged; a merged follow-up contributes its own size once.
  These are task change sizes, not net repository growth: overlapping changes,
  later reversions and merge conflict resolutions can differ from the sum.
- Completed verifications, from tasks of kind `verify` with outcome `done`, by
  `finished` time. Their `verdict` counts as held (`holds` or `holds, with notes`),
  did not hold (`does not hold`) or unsure (`unsure`). A missing verdict contributes
  only to the verification total. Closing a verification retains its result.
- Verifications that held: the share held among the verifications with a verdict,
  held, did not hold or unsure (`held` of `verdicts`), shown with its counts. With
  none it shows "—".
- Releases asked for, from `.hansa/releases.json`'s `asked` time, including refused
  and failed requests; releases that went out, from records with state `done`, by
  `ended` time.
- Worker and verifier hours, from each task's `started` to `finished`, and every
  retry or recovered run's `ran_from` to `ended` in `attempts` and `earlier_runs`.
  Runs without token usage still contribute time; runs without both timestamps
  cannot. A run's entire duration counts in the period it ended, even if it
  started earlier. Concurrent runs add their hours; checks and release commands
  are not worker time.
- The coordinator's turns with a step in the period (`coordinator_turns`), and
  the time it worked in them (`coordinator_hours`): each turn from its first
  step in the period to its last, from the coordinator's session records, as
  `hansa cost --turns` counts them ([What work costs](tasks.md#what-work-costs)
  says which sessions count).

Periods are rolling windows of N × 24 hours, including the starting instant.
All history uses the same records without a lower time limit and reads no git
history, the coordinator's and its helpers' session records included (its
working folder's from when the desk began): hansa
reads them once for the usage and the turns, about 3 seconds for 500 MB of
records (a busy coordinator's month) on a laptop. `/api/cost?days=all` selects all history (`days: null` in
the response); `days=90` selects 90 days. Both use the usual page key, including
`&rows=1` for CSV data. `work` holds the counters, including `lines_added`,
`lines_removed`, `lines_omitted`, `merged_first_try`, `verdicts`,
`usd_per_merged` (null when nothing merged), `coordinator_turns` and
`coordinator_hours`;
`work_chart` supplies the work chart's `interval` and `buckets`. CSV retains its usage rows and columns;
all-history export is named `cost-all.csv`.

Unpriced models and a coordinator that is not counted remain explicit warnings:
`coordinator_uncounted` says why, `coordinator_uncounted_kind` names the reason
(`user`, `untied`, `login`, `missing` or `unreadable`) and
`coordinator_uncounted_where` the folder or file it names, for the page's
translation. Dollar values are an estimate, not a bill. A
bar chart above the day table shows each returned day's USD value at the
principal's prices, in date order. Each bar's accessible label and tooltip give
the full date, amount and any unpriced token count; a warning mark identifies
days whose tokens are partly or wholly unpriced. Narrow screens keep all bars,
with fewer visible date and amount labels. The last label is anchored at the
right with a fixed share of the chart width and unwrapped text so it stays
readable above the table. The day table is unchanged. The chart
uses `/api/cost`'s `by.day` date keys, `usd` and `unpriced_tokens`; it does not
price tokens in the browser. Dates missing between the first and last returned
day appear as empty bars with their date and no amount, using calendar arithmetic
on the date keys alone. The chart does not extend beyond those dates: the API's
cost day rows supply no calendar boundaries or desk timezone for the period's ends.

A separate work chart below the cost chart shows finished and merged task
counts, using the same outcome rules and period boundaries as the totals.
For 1, 7 and 30 days it has exactly that many rolling 24-hour bars, labeled by
the ending date. For 90 days and all history, it groups work into calendar weeks
starting Monday in the desk's local time, with partial first and last weeks.
All history starts with the earliest recorded finish or merge (or today for an
empty desk). Empty intervals retain a zero bar. The upper segment counts tasks
finished and the lower counts tasks merged; a task can contribute to both.
Hovering or focusing any bar shows its local start and end times and both
counts as text; its accessible label carries the same information. The work
chart also appears when there is no token usage. Counts are supplied by the
API, rather than derived from usage rows or git history.

A period without usage rows shows the usage empty state. A failed range change
identifies the old report that remains visible and disables
export until a report loads successfully. Retry requests the selected range.
CSV export keeps the original rows and columns, uses the range selected when the
export started, and reports success or failure. Text cells starting with `=`, `+`,
`-` or `@`, including after leading whitespace or ASCII control characters, and
cells starting with a tab, carriage return or newline receive an apostrophe prefix
so spreadsheets treat them as text. Keep this prefix when opening the CSV in a
spreadsheet; actual numeric values stay numeric. Both pages share the settings
page's native CSS and JavaScript controls and reduced-motion support.

The page keeps navigation for the current browser tab in `sessionStorage`:
`hansa.tab` is written whenever Status, Goals, All tasks, To-dos, Gaps, Notes, Settings or Work and cost is shown (including
startup), `hansa.settingsSection` whenever a Settings section is shown, and
`hansa.costPeriod` when the Work and cost range changes. Reloading restores those choices;
a fresh browser tab starts on Status. An address containing `?t=` (or `&t=`)
always opens Status, where releases and to-dos wait for the principal.
Unknown saved choices fall back to Status, Providers and 7 days.
Scroll position and expanded Status details are not stored.

The globe button at the end of the header opens a small menu offering `中文`
and `English`, with the current language marked. Clicking elsewhere or pressing
Escape closes it. Opening either header menu closes the other.
Choosing writes `hansa.language` in `localStorage`
(`zh-CN` or `en`) and reloads, keeping navigation. Unsaved Settings text triggers
the browser's usual warning before leaving; accepting the reload discards it.
If the viewer stays, the language choice applies on the next load.
The choice applies across visits and browser tabs;
without a valid choice, a browser language starting with `zh` selects Chinese,
and other languages select English. The HTML `lang` attribute matches the shown
language. If storage is unavailable or throws, the page still works with its
usual defaults for the choices it cannot read or keep.

### Notes on the web page

Notes shows files directly in the desk's `notes/`, most recently modified first,
with their size, modification time and the Markdown headings that `hansa notes`
lists (headings inside fenced code are ignored). Show archived also includes files
directly in `notes/archive/`. A missing or empty folder shows No notes. Choosing
a file shows its text as plain text, including Chinese; Markdown and HTML are
never rendered. The preview contains at most the first 1 MB (1,048,576 bytes),
with a truncation notice and any incomplete final UTF-8 character omitted.
Files that contain invalid UTF-8 or NUL bytes are listed as non-text and cannot
be displayed. hansa removes known keys from names, headings and previews, as it
does from reports. Browsing never changes the notes.

`GET /api/notes` lists these files; `archived=1` includes the archive. Each record
has `name`, an opaque `id`, `size` in bytes, `modified` in Unix seconds, `binary`,
`truncated` and `headings` (zero-based line, level, text). `GET /api/note?id=ID`
returns the metadata and preview `text`; `name=FILE` also accepts a filename or
`archive/FILE`. Both requests require the usual bearer page key. Invalid paths
and unavailable files return HTTP 404. Other subfolders, absolute paths and `..`
are refused before opening anything. Reads open directories and files without
following symbolic links and check the opened file is regular with exactly one
hard link. These checks prevent note browsing from exposing files elsewhere;
keep them while the page reads notes from the filesystem. Headings and UTF-8
validation cover the whole file even when its preview is cut.

## Settings on the web page

The read-only **Changes** section starts folded. Opening it shows the newest 50
settings changes: time, actor or command, setting path, and old → new values.
**Show 50 more** extends the list; filter by a lane or provider name, including a
removed one. Withheld values stay withheld, and long values retain their recorded
truncation markers. History loads only when opened and refreshes at most once a
minute while visible and open, separately from Status. `GET /api/history` uses
the page token, gzip and ETags, with `last` (default 50) and optional `kind`
(`lane` or `provider`) plus `name`; invalid filters or limits return HTTP 400.
It returns `recording_since` and `entries`, shared with `hansa explain history`.

Settings are grouped into Providers, Coordinator, Lanes, What workers
may not read, Releases, Alerts and Prices. Select a group in the directory; full
explanations and other read-only lane fields expand in place. Network, write and
allowed-command rules remain visible, as do the warnings needed before saving.
Providers includes the rest minutes that apply to every provider.
Lanes starts with Dispatch control, above the read-only concurrency and disk floor.
It shows whether dispatch is on or paused, including the pause reason. Pause sends
the optional reason immediately; Resume also takes effect immediately. These
buttons work separately from Save settings and preserve all unsaved settings.
The dispatch state loads when Settings opens and after either action. A failed
press shows its error and keeps the displayed state and reason box. The header
still reports when the desk is paused.
The save bar applies to all editable settings groups.
In **Settings → Lanes → General**, **Desk name** (`desk_name` in
`settings.json`) lets the principal name the desk in any language, for example
`网店台子`. Saving strips surrounding spaces; only spaces saves an empty name.
It accepts at most 40 characters on one line, without control or Unicode format
characters (including invisible direction overrides and zero-width spaces).
A name containing a saved provider key, push secret or page key is
refused before anything is saved, so a pasted key cannot become a public heading.
Only the principal's save on the page changes it; commands cannot set it.
Settings history records both setting and clearing the name. An older page or
command that omits the field keeps the saved name.

An empty or unset name uses the last two folders of the desk's path, such as
`shop/desk`. A name written by hand is used only if it is a string that passes
the same character and saved-key checks as a save; otherwise the folders are used.
If settings cannot be parsed, the desk list still shows its server, page address
and counts with the folder-based name. The name is used in alert headers, push titles and groups, the
page's browser title and header, and the first line of `hansa status`.
`hansa desks` and `hansa menu` show both the name and folder, so desks with the
same name remain distinguishable. Changing the name does not rename its folder
or tmux server.

In Alerts, beside the alert command, **Alert language** (`alert_language`) selects
English (`en`, the default) or Chinese (`zh`). Before it has been saved, the select
prefills from the page's language; alerts stay English until the principal saves
the choice. Only the principal can save it, and settings history records it.
Other values are refused on save; an invalid value in `settings.json` reads as
`en`. A save that omits `alert_language` keeps the saved choice; if it was never
saved, it stays unset and the page still prefills it from its own language. This
keeps an older page or a request for another setting from changing alert language.
A saved language change also applies to alerts still gathering.

Push addresses send alerts to phones and chats through push apps such as Bark.
Add, save and test up to ten entries in **Settings → Alerts → Push addresses**.
Under **Add an address**, paste the whole address from your push app or service.
Leave **Recognize it** selected, or choose a service and follow the service's hint.
Press **Save settings**, then **Send a test push** on the saved entry.
For Bark, copy the address from the app's home screen.
See [push-address setup](releases.md#push-addresses-and-services) for the page's
controls, and the [service guide](push-services.md), including
[Bark](push-services.md#bark), for what to paste.

Only the principal's page save adds, changes or removes push addresses. Settings
(`alert_push`) keep metadata; each address is a secret in a separate 0600 file
under the desk's 0700 `secrets/` folder. Services needing an extra signing secret
or access token get the same protection. Hansa removes the keys it knows from
new records and displayed text. Status and the page show a hidden address and
each entry's last result. Saving a secret scrubs the task, request and try files
covered by `Desk.scrub` and protects later merges. The append-only event log
keeps text recorded before hansa knew the key; see the
[redaction limits](releases.md#push-addresses-and-services).

Hansa tries to move an old Bark setting and key to the first push entry,
keeping the on/off choice. A successful move removes the old files and records
the move in settings history. Without a saved key, no entry is added.
If the move is refused, hansa keeps the old setting and key without sending to
it. Status and the page ask the principal to paste and save the Bark address
again in **Settings → Alerts**. See
[Bark upgrade and repair](releases.md#push-addresses-and-services) for the
refusal cases and what a repair removes.

Every enabled entry with an address receives each gathered batch, even with the
alert command blank. Sends run independently. One-time release codes go only
to the alert command; answer push alerts about waiting releases on the page.
See [alerts](releases.md#alerts) for gathering, deadlines, levels and failure
records, and the [service guide](push-services.md) for what leaves this machine
and who can read it.

Sandbox alerts explain which runs are held and how they resume in the selected
language, and keep the sandbox's raw diagnostic alongside that explanation.

Prices must be finite numbers, zero or more; zero and decimals are accepted.
NaN, infinity and numbers too large for floating-point calculations are refused;
use a smaller price. A saved file containing such prices also requires repair
before hansa uses it, following [the recovery steps](#reading-and-restoring-settings).

Missing cache-read and five-minute cache-write prices default to 0.1 and 1.25
times the input price. For models whose names start with `claude-`, a missing
`cache_write_1h` is twice the input price, even when `cache_write` is set.
For other models it uses `cache_write` when given, otherwise twice the input
price. An explicit `cache_write_1h` always wins. Usage without a five-minute /
one-hour split is counted as one-hour writes.

After saving, it describes the timing for each kind of setting changed:
prices apply immediately on the next report read, recalculating past usage too;
provider settings apply to new worker/verifier runs and the next coordinator start;
rest minutes apply when a new rest begins, keeping existing rest end times;
not-read rules apply at the next worker, verifier or coordinator start, leaving
running processes' access as it was. Coordinator model, provider, effort and
working compaction apply at its next start; idle compaction reads its threshold
at the mod's next decision. Release rules apply at the next relevant task-title,
merge or release check, and alert settings at the next alert check.
The visible not-read warning says that while the list holds an entry containing
`()[]{}*?\`, hansa starts no worker, verifier or coordinator until it is changed here.

`hansa ping [PROVIDER]` sends a tiny request through one provider, or all when
omitted. From a shell it uses the `claude` program the recorded `hansa serve`
start used, and says when that differs from the one the shell would find.

The directory marks groups with unsaved edits. The save bar distinguishes edits,
an operation in progress, success and failure; a failed save says that the draft
is still present. Known validation errors link to the relevant field or group,
with field errors shown beside the control. Narrow screens use a horizontal
group directory. Controls have visible keyboard focus, and brief transitions
follow the browser's reduced-motion preference.

Switching between Status, All tasks, Settings and Work and cost, or between settings groups, keeps
unsaved edits. The page marks them and asks the browser to warn before a refresh
or close. Drafts live only in page memory: they do not survive leaving the page.
Save settings writes them; a failed save keeps them. During a save the settings
form is disabled until the saved settings have been reloaded. Discard confirms
before replacing a draft with the desk's latest settings; with no draft it simply
reloads them. If a save meets newer settings, the server refuses it. The page
reloads the latest settings and keeps each top-level setting changed in the principal's
draft, taking the others from the new copy. It names the settings changed
elsewhere and those changed by both using the page's group names in its language,
and shows any problems returned with the new copy alongside the notice. The principal's
draft value is kept for settings changed by both. Typed
keys remain. Nothing is saved until the principal presses Save again. A failed reload keeps
the draft too. Unsaved markers compare values regardless of object key order;
a blank optional field counts as unset. Names typed for a new provider or price
also count as drafts, even before Add. If a save succeeds but its reload fails,
the page says that the save succeeded.

Keys are saved separately with each provider's Save key button. An unsaved key
input stays in memory across page and group switches, and across a settings save;
it is never included in the settings request or put in browser storage. A
confirmed discard clears those inputs too. Keys already saved are unaffected.
The eye button shows or hides the current key input without saving or changing it.
Visible notes beside the input explain [storage and use](#where-a-desks-files-are)
before saving. HTTP sends keys without encryption: use HTTPS for remote endpoints
and reserve HTTP for trusted local adapters. The page warns rather than refusing HTTP.
With no draft, returning to Settings refreshes it from the desk. If the principal starts
editing before that reply arrives, the reply leaves their new draft alone.

## Starting the server and the coordinator again

In a terminal, `hansa` alone or `hansa menu` opens the principal's list of all
this account's desks, even from inside a desk or with `--desk` or `HANSA_DESK`.
Nothing starts when it opens. Each row shows the saved desk name, or the last
two folders of its path when no name is set, and the full path,
server state (running, busy, stuck, stopped), whether the coordinator's session
is open, whether dispatch is paused, running tasks and tasks awaiting review,
and releases and to-dos waiting for you. It refreshes every five seconds.
Above the keys, a guide explains the chosen desk's next step and how to attach and return with Ctrl+B then D while the coordinator keeps running; after `s`, a fresh summary gives each desk's result and attach command before waiting for a key.

| Key | Action |
|---|---|
| Up / Down | Choose a desk |
| Space / `a` | Tick this desk / tick all (or untick all) |
| `s` | Start the ticked desks, or the chosen desk if none are ticked; clear ticks afterwards |
| Enter | Enter its recorded tmux session; Ctrl+B then D returns to the menu |
| `w` | Run its own `hansa --desk DIR web`; Ctrl+C returns |
| `x` | Ask for `y`, then stop its server with TERM; its coordinator stays open |
| `p` | Run its own `pause "hansa menu"`, or `resume` when paused |
| `r` | Refresh now |
| `q` / Ctrl+C | Leave; running work stays running |

`s` starts what is stopped, server then coordinator, through each chosen desk's
saved hansa installation; desks not chosen are left alone. `s` and `p` show the
command's output as it comes and wait for a key before returning. Ctrl+C while
a command runs gives it up to five seconds to end by itself before killing it
and returning. A second Ctrl+C during that wait kills it immediately and returns.
Drawing the list runs no tmux command and writes nothing in a desk. Enter uses
`switch-client` inside the same tmux server; for another server it asks you to
detach first and gives the attach command. An attached session can still be
entered from elsewhere.

`x` refuses while tasks, verifications, merge checks, releases or lane tries
run, and lists their kinds and counts. Use `p` to keep new work from starting,
then stop once that work ends. After TERM it waits up to 30 seconds, reports
whether the server stopped, and never sends KILL. A missing desk folder is
shown and skipped. An empty list explains `hansa init`; a broken list shows
the same problem as `hansa desks`.

Use `hansa menu --lang zh` or `--lang en` to choose Chinese or English.
Otherwise the first set of `LC_ALL`, `LC_MESSAGES`, `LANG` chooses Chinese if
it starts with `zh`, English otherwise. The screen uses UTF-8. `hansa menu`
requires a terminal on stdin and stdout and refuses agents and Claude Code
sessions; `hansa` alone prints usage in those conditions. Over SSH, `w` first
shows the tunnel command needed on the computer with the browser, using this
desk's page port, then lets the desk's own `web` print its address and key.

For first startup and the alternative without tmux, follow
[Getting started](getting-started.md#create-and-start-the-desk). This section
describes saved starts, port selection and restoring sessions.

Run `hansa up` in a desk, or name it with `hansa --desk DIR up` or `HANSA_DESK`.
If the named folder is not a desk, hansa refuses it and starts nothing; it does
not fall back to the account's desk list.
It starts the server as it last ran, then the coordinator's session as it last
started ([below](#the-coordinators-session)). A running server, including a busy or stuck
one, is left running and its pid is shown. It does not arrange startup at boot,
or stop or restart a running server or session.
Whether a server runs is asked of the lock hansa serve holds; where that lock cannot be
asked, of the process it names. Where the lock names a start time, a lookup of that process
that fails for a reason other than that there is no such process (`EPERM`, `EACCES`, `EIO`,
say) does not make the server stopped, so nothing starts a second server over one hansa
cannot look at: only `ESRCH`, or a start time that reads differently from the saved one,
says it is gone. A lock naming only a number, as older servers wrote it, is still judged by
the server's last round.

Each `hansa serve` start saves `.hansa/serve-start.json`: the absolute path of
its installation's `bin/hansa`, `--host`, `--every`, whether `--port` was given
(and its value if so), and the variables
that were set: `PATH`, `LANG`, every `LC_*` (including `LC_ALL`), `HANSA_CLAUDE`,
`HANSA_TMUX`, `CLAUDE_CONFIG_DIR`, `TMPDIR`, `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`
and `TMUX_TMPDIR`. In tmux it also saves the server and session. A socket in
tmux's own directory is saved by server name (`-L`), so tmux can recreate the
directory after a reboot; a custom socket keeps its path (`-S`). A manual
foreground `hansa serve --port N` works as before and updates this record.
`hansa serve` is the dispatcher and web server that `hansa up` runs.
The command path keeps links to the installation, so switching such a link
makes future starts use the new installation.

Without `--port`, `hansa serve` tries the port in `.hansa/web-port`, or 8765
before its first run. If it is taken, it tries each following port until one
binds (up to 65535). It prints the chosen port, says which port was taken when
it moves, and saves the chosen port in `.hansa/web-port`;
`hansa web` and `hansa status` then use that address. The next start tries that
saved port first, even if 8765 is free again. The start record preserves automatic
port selection, so `hansa up` follows the same rule. An explicit `--port N`
stays explicit in the start record and fails if N is taken, as before.
Older records did not distinguish an explicit 8765 from the default: `hansa up`
treats an unmarked record's 8765 as automatic, keeping other recorded ports
explicit. New records distinguish both, including an explicit `--port 8765`.

The record excludes `ANTHROPIC_*`, other key variables, values holding a key
hansa knows when saving, and Claude Code session identity or messaging variables.
Hansa removes the session's identity before recording the start.
See the [redaction limits](releases.md#push-addresses-and-services) for text
recorded before a key was saved.
The restarted server gets only `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM` and
the recorded variables, plus tmux's own pane variables when in tmux. `USER` and
`LOGNAME` come from the current account's password entry, regardless of the
calling shell's values. `HOME` keeps the shell's value when set. It does
not inherit the other variables from the shell running `hansa up` or from an
existing tmux server.

With tmux installed (found by hansa's usual lookup, including `HANSA_TMUX`),
`hansa up` starts a detached pane in the saved server and session. If that
session already exists, it adds a window there. Without a saved tmux location,
it uses a server named `hansa-<desk folder name>-<hash>` and session `serve`.
The folder name is made safe and limited to 32 characters; the hash is the first
12 hexadecimal characters of SHA-256 of the desk's real path, so the name stays
the same on each start and separates desks with the same folder name. The
coordinator uses this server too, in session `coordinator`, unless a tmux location
was saved. Use the attach command `hansa up` prints for that desk.
The pane command uses `/bin/sh`, regardless of the account's shell.
Output stays in the pane and is also appended to `.hansa/serve.log`.
Without tmux it starts a background process in its own session, with input
from `/dev/null` and output appended to that log. Before either start, a log
over 20 MB moves to `serve.log.1`, replacing the previous backup.

With no saved start, hansa says so and uses the defaults and the current
shell's allowed launch variables. After starting it waits up to eight seconds
for the server's lock and page to be ready, then prints the pid, page port and
tmux attach command (or a command to follow the log). A failed start shows the
log's last lines. On another account's desk, it starts nothing and gives the
command to run as that account, so the server uses that account's tmux and home.

Each server start also adds or updates one entry for its desk in this account's
`$XDG_STATE_HOME/hansa/desks.json`, or `~/.local/state/hansa/desks.json` when
`XDG_STATE_HOME` is unset. That list holds the desk folder, the absolute hansa
command and when it was last listed. If the list cannot be updated, `hansa serve`
warns on stderr and keeps serving; the desk's own start record is still saved.
A list that is there but cannot be used (empty, not JSON, not a JSON list, a
link that leads nowhere, unreadable) is left as it is, with the reason on
stderr: rewriting it would drop the other desks, whose runs wait until the
list is fixed. A missing list is made.
Outside any desk, with no `--desk` or `HANSA_DESK`, `hansa up` opens this menu
when stdin and stdout are terminals. Without those terminals, or with
`HANSA_ROLE`, `HANSA_TASK` or Claude Code session variables set, the menu cannot
open: `hansa up` starts nothing and says so, lists the account's desks by folder,
and gives the two forms that work, `hansa --desk DIR up` and `hansa up --all`.
`hansa up --all` visits every listed folder that is still a desk, printing a
block for each; it is refused inside a desk or when `--desk` or `HANSA_DESK`
names one. Each is started by its own installation with
`<saved hansa> --desk <folder> up`. If that installation is missing or too old
to have `up`, nothing starts there and hansa prints the last server command
instead. `hansa init` puts the new desk on this account's list, stopped. If the
list cannot be read or written, init still makes the desk and warns in one line;
its first server start (`hansa up` in the desk) tries to list it again. With
`init --user`, listing runs as the desk's account, as a server start does.
Each desk's runs are kept from the other listed desks and from the list itself
([sandbox.md](sandbox.md#what-workers-and-verifiers-may-touch)).

`hansa desks` shows this account's whole list from any folder, including inside
a desk. It writes nothing in any desk. Each block gives the name and folder, whether
its server runs (including busy or stuck), the page address without its key,
the running server's recorded hansa version, and counts only. A server without
a matching version record shows `unknown`; a stopped server has no current
version. For example:

```text
desk work/desk-one (/work/desk-one)
  server: running (up); hansa 1.2.3
  page: http://127.0.0.1:8766/
  tasks: 2 running, 3 waiting, 1 waiting for review
  principal: 1 releases, 2 to-dos waiting

desk work/desk-two (/work/desk-two)
  server: not running; hansa unknown
  page: http://127.0.0.1:8765/
  tasks: 0 running, 1 waiting, 0 waiting for review
  principal: 0 releases, 0 to-dos waiting
```

Running and waiting count task records in `running` and `queued` respectively,
including verifications. Review counts `done` tasks, excluding verification
records, as `hansa status` does. Releases count those waiting for approval;
to-dos count open requests. A stopped desk still shows its counts and its last
page address (or the default before it has served). Missing or unreadable desk
folders get one line each, and the other desks are still shown. A missing or
empty list says `No desks listed`; an unreadable list gives the same reason as
`hansa status`. `hansa desks --json` gives the same facts, including `desk_name`
and the full folder in `desk`, in `desks` and a
list-level `problem` string. Neither format includes task titles, notes,
reports, to-do texts, provider keys, release codes or the page's key.

`hansa desks --forget PATH` takes that one entry off the list, leaving the desk's
folder and files and every other entry as they are. It refuses while that desk's
server runs, saying to stop it first, and says so when `PATH` is not on the
list; both change nothing and exit 1. An entry whose folder is gone can still be
taken off this way, so removing a desk takes three steps: stop its server (`x` in
the menu), `hansa desks --forget DIR`, then delete `DIR`. The lock the list is
written under is the one `hansa serve` takes, so a start and a forget cannot
leave two versions of the list.

### The coordinator's session

Each start of the coordinator's session saves `.hansa/coordinator-start.json`;
the latest start wins.

- `hansa coordinator [words]` saves hansa's own option (`--clear-daily`), the
  words it gave claude, the folder (the desk), the tmux server and session when
  it runs in tmux, and the launch variables listed above.
- A session hansa did not start (in a folder of its owner's, with its own
  `CLAUDE.md` and memory) saves its start when it runs `hansa notify on`: the
  exact argument list of its claude process (the `CLAUDE_PID` process; read
  through sysctl on macOS, `/proc/PID/cmdline` on Linux), that process's
  folder, the tmux server and session (`TMUX` and `TMUX_PANE` reach the command
  from the session), and the launch variables of the command's own
  environment, which it has from the session. `hansa notify on` then adds one
  line: after a restart, `hansa up` starts this session again in FOLDER with
  COMMAND. Run in the session the record says `hansa coordinator` started (the
  same pid and start time), it saves nothing: that start's record stands.
  `HANSA_ROLE` does not decide this: a desk owner's wrapper script commonly sets
  `HANSA_ROLE=coordinator` on every hansa command, so a session it runs
  `hansa notify on` for is recorded like any other.
- Both save the session's process: its pid with its start time, as hansa tells
  sessions apart.

The record excludes keys hansa knows when saving and the session's identity;
see the [redaction limits](releases.md#push-addresses-and-services).
The words that pick a conversation or give an inbox are left out with their
values: `-c`, `--continue`, `-r`/`--resume`, `--session-id`, `--fork-session` and
`--messaging-socket-path`. So a session started with `--resume <its id>` or
`--session-id <its id>` is recorded, and so is one `hansa up`'s launcher
started (it carries its inbox) when it runs `hansa notify on`: it hears from
hansa already, and running it does no harm. When a word that is kept holds
one of the desk's keys or the session's identity, or the argument list or the
folder cannot be read, no command is saved and the start says why. The record
then holds only that and the session's process, so `hansa up` starts no older
command and says why; only a session already recorded keeps its record when a
later `hansa notify on` cannot read it. As root on an account's desk nothing
is saved: root writes nothing there, and `hansa up` runs as the account.

Claude Code loads the card (the desk's `CLAUDE.md`) only for a session started
in the desk or below it, or one whose `CLAUDE.md` imports it. So
`hansa notify on` looks at the `CLAUDE.md` and `.claude/CLAUDE.md` in the
recorded folder F and the folders above it for a line `@path` naming the card
(a relative path read from the file's folder). With none, it writes a copy of
the card to `F/.claude/rules/hansa-card.md`, which every Claude Code session
started in F or a folder below it reads at each start; this session reads it
from its next start, and until then `hansa help card` prints the card. Above
the card the copy has hansa's header: hansa wrote it for this desk, rewrites
it, and removes it, so edits to it are lost; and the session was not started
by `hansa coordinator`, so the helpers (scout, reviewer, operator) and the
coordinator's mod are not in it, but for hansa's status line when `hansa up`
started it, and where the card says to ask a helper it
makes a task. The header also says the card is for the session that coordinates
that desk, the one hansa posts its notices to, and any other session started
in that folder or below it, such as a worker a script starts or a session for
other work, must ignore it. The card's import of `notes/PROJECT.md` names that file from
the copy's folder when the desk is inside F; with the desk outside F the copy
holds a pointer to the file in its place, and imports nothing. The copy is
always made from hansa's own card, never from the desk's `CLAUDE.md` as it
stands. hansa rewrites the copy at every `hansa notify on` and wherever it
writes the desk's card: at `hansa init`, at each `hansa serve` start (which
brings the desk's card up to hansa's, as `hansa coordinator` does, so a desk
whose coordinator only runs `hansa notify on` gets an upgraded hansa's card and
copy too) and at `hansa coordinator`'s start, which instead removes a copy in a
folder that holds the desk: the session it starts there would read both.
`hansa notify on --no-card` records the session but removes hansa's copy and
keeps the copy off for this desk, including later plain `notify on` and starts
of `hansa serve` or `hansa coordinator`. `hansa notify on --card` turns it back
on and writes the copy as above; the default is on.
`hansa notify off` removes hansa's copies, and `hansa notify on` from another
folder removes the earlier one; only a file with hansa's header for this desk
that git does not track is removed. When F is in a git work tree, hansa adds
the copy's path to that repository's `info/exclude` (`git rev-parse --git-path
info/exclude`), once, and not when git already ignores it; it never writes a
file git tracks, as a copy committed into a project would reach every session
of everyone who clones it. hansa's own runs leave the copy out: each worker
and verifier run lists, for every folder above its checkout, that folder's
`CLAUDE.md` files and `.claude/rules/**` in its `claudeMdExcludes`.

hansa writes no copy, and says why, when a file without its header is at that
path (it is left), when git tracks the path, or when F cannot be written: it
then gives the line to add to F's `CLAUDE.md`: the card's path relative to F
when the desk is inside it (such as `@desk/CLAUDE.md`), else its absolute path,
which Claude Code loads only once imports from outside the folder are allowed
(it asks once at a start: "Allow external CLAUDE.md file imports?"); hansa
never writes that line, the folder being another project's. Nor does it write
a copy when F's `.claude` is the account's Claude Code folder (the home
folder's, or `CLAUDE_CONFIG_DIR`'s) or F holds the home folder: rules there
reach every session of the account. It then gives no line either, as a
`CLAUDE.md` there reaches every session below it, and says to start the
coordinator in a folder of its own or with `hansa coordinator`. A line already
there with the card outside the folder gets the note on external imports, and
no copy. `hansa status` adds a short note to its `hansa posts to` line (with
the copy in place, that the session reads the card from it), and `hansa explain
notify` the full lines. A session `hansa coordinator` started runs in the desk
and is not checked.

After the server, `hansa up` starts nothing when a session hears from hansa,
and names it (pid and name). It starts nothing either when the recorded
session's process still runs (the same pid, started at the same time), even
one that does not hear from hansa: it says to run `hansa notify on` in it,
since a second session would continue the same conversation. A process name
does not find that process: Claude Code's shows its version as its name. A
record whose words hold `-p` or `--print` is not an interactive session:
nothing starts, and `hansa up` says so.

Otherwise it starts the session in tmux: in the recorded tmux server and
session, or, with none recorded, the server `hansa up` uses for the desk's
server and a session `coordinator`; when that session exists, in a new window.
The pane gets the server's clean environment (above), with the recorded
variables, and runs in the recorded folder:

- a `hansa coordinator` record: `<hansa> --desk <desk> coordinator <recorded words> --continue`;
- a `hansa notify on` record: the recorded argument list, with `--continue`,
  through hansa's launcher (`hansa notify-launch`, not listed in help). The
  launcher starts the follower for its own process, as `hansa coordinator`
  does, records the start again, then execs claude with
  `--messaging-socket-path`, so the session hears from hansa without
  `hansa notify on`. It runs outside the desk and reads none of the desk's
  settings, so the launcher adds deny rules (`launch_rules` in
  `hansa/cli.py`): the desk's push and publish rules (the same list), Read on
  its `secrets/` and `.hansa/`, Edit on each of its files but `notes/` and
  `worktrees/`, by full path, and Read on the logins and keys under the home
  folder, as `hansa coordinator` gives its session. They go into the recorded
  command's own `--disallowedTools` list when it has one; its other options
  stay. A rule Claude Code would split stops the start, as for `hansa
  coordinator`. Not added: the principal's list of what workers may not read
  (it may hold what this session's work reads), the no-edit rules on the lanes'
  repositories and worktrees, and turning off helpers it does not have. A
  session started otherwise has none of these rules until `hansa up` starts
  it; `hansa notify on` and the copy's header say so. The launcher also adds
  the turn hooks below with `--settings '{"hooks": ...}'`, unless the recorded
  words give a `--settings` of their own (then it adds nothing: the two are
  not merged).

`--continue` continues the most recent conversation in that folder. hansa's own
`claude -p` runs (`hansa ping`) use a temporary folder, so they leave none
there. A prompt given on the recorded command line is given again.

With no record on a new desk (no task, and nothing under `.hansa/followers/`,
as after `hansa init`), it starts `hansa coordinator` in the desk, without
`--continue`, and says that the first start in a folder asks whether to trust
it: attach and answer. With no record on any other desk, it starts nothing:
that desk had a coordinator before hansa recorded its start, maybe a session
in another folder that ran `hansa notify on`, and a fresh `hansa coordinator`
would be a second coordinator, in the wrong folder, with a new conversation.
It says that the start is not recorded yet, and what to do: start the
coordinator as before and run `hansa notify on` in it, or start it with
`hansa coordinator`; from then on `hansa up` starts it again. After a start, it waits
three seconds and prints that the session started, with the attach command
(`tmux -L NAME attach -t SESSION`, or `tmux attach -t SESSION` for tmux's
default server); if the pane ended, it says that the start failed, with the
command to run by hand. Without tmux it starts no coordinator, and prints the
command to run in a terminal of one's own. On another account's desk it starts
nothing, as for the server. While no session hears from hansa, `hansa status`
says in one line that `hansa up` starts the session again, and how, or why it
does not, including a start not recorded yet and what to do about it, and a
recorded folder that is not there.

### How each of the coordinator's turns ended

When the API keeps failing, Claude Code retries, then ends the turn with an API
error and sits idle. So that this can be noticed, `hansa coordinator` and the
launcher give the session two hooks, `Stop` (a turn ended normally) and
`StopFailure` (an API error ended it), each running hansa's own
`bin/hansa --desk <desk> coordinator-turn --pid <N>` (not listed in help). `N`
is the pid of the process that execs claude, so the session's pid. The hooks
are not async: a turn may end right after another, and each record counts on
the one before it; the command takes about a third of a second.

`coordinator-turn` writes `.hansa/coordinator-turns/<N>.json`, whole, in one
rename, without the desk's lock:

    {"at": <time>, "ok": true|false, "error": <type>|null, "fails": <failures in a row>,
     "failed_since": <time of the first of them>|null, "last_ok": <time of the last ok turn>|null}

`error` is Claude Code's error type (`rate_limit`, `overloaded`,
`authentication_failed`, ... `unknown`); a type not on its list is kept as
`unknown`. Nothing else of the hook's input is kept: not the last message, the
error's details, the transcript's path, the folder or the session's id.
Missing or malformed input writes nothing, and the command always exits 0. As
root on an account's desk, root passes only the event and the error type to the
account's hansa, which writes the record. `hansa serve` removes, as it starts,
the records of sessions whose process is gone (`up.sweep_turns`); it leaves
alone any file there whose name is not a pid's record (`<pid>.json`, the pid
plain digits from 1 to 4194304), and the sweep never stops the server.
Records are keyed by pid alone, so before it execs claude, `hansa coordinator`
or the launcher removes any record for its own pid, the one it gives the hook:
a dead session's record is never the new one's (as root on an account's desk,
the account's hansa removes it).

`hansa notify on`, in a session the launcher started, leaves that `--settings`
out of what it records, as it leaves out the deny rules, and keeps it in the
record as `launch_settings`; so a session started, recorded again and started
again carries one copy. `hansa status` says, after its `hansa posts to` line,
whether that session's turns are recorded, by the record of how it started
(`.hansa/coordinator-start.json`, when it is of that same session: the same pid
and start time). `hansa coordinator` and the launcher write `"turn_hooks": true`
in it when they gave the session the hooks, and status says recorded only then.
A start record without it is of a session started before hansa gave its
sessions these hooks (hansa updated while it ran): not recorded, until its next
start by `hansa coordinator` or `hansa up`. A session started by hand and
recorded with `hansa notify on` is not recorded until `hansa up` starts it; one
whose command gives a `--settings` of its own is not.

## A desk's files

### Reading and restoring settings

If an existing `settings.json` cannot be read as a UTF-8 JSON object, commands
that need it refuse and the page shows recovery instructions (API: HTTP 503,
`code: settings_unavailable`). Settings saves leave the file untouched. Stop
`hansa serve`, keep a copy of that file, then restore a known-good copy from
your own backup, or repair the JSON and its read permissions. Keep the desk's
task folders and the original lane names; restoring the settings does not
rewrite tasks. Restart the server and reload the page after repair. Recovery
is manual: the page does not replace the file or create a backup. A missing
settings file still uses the defaults, as when creating a desk. These checks
keep hansa from acting on incomplete settings and remain necessary while
settings are stored in this file.

### Where a desk's files are

The desk's files are under its directory; see the top of [`hansa/desk.py`](../hansa/desk.py).
The account's desk list lives outside it, as described [above](#starting-the-server-and-the-coordinator-again).
A write that fails leaves the previous file, with its permissions, and no temporary file.
The [settings history](concepts.md#settings-history) is kept separately in
`.hansa/settings-history.jsonl`; keep it when copying or restoring the desk.
The event log is only appended to. Readers skip a damaged line, and the next event starts
on a line of its own. Keys live
in `secrets/` (mode 0700), in files with mode 0600; the OS account running hansa
can read them. The worker or adapter receives the selected key in its environment
for requests to the configured provider endpoint; a local adapter controls how
it uses that key.
Saved values are not returned to the page. hansa replaces a known key with
`[key of <provider>]` wherever it would otherwise show, and refuses to merge a
change that holds one. Earlier event-log entries are not rewritten when a key
is saved; see the [redaction limits](releases.md#push-addresses-and-services).
The coordinator's permission rules are hansa's: they are
in the desk's `.claude/settings.json` and come up to date whenever the
coordinator starts. The principal's own rules go in `.claude/settings.local.json`.
The coordinator creates and edits files under the desk's `notes/` without
asking (its plan and findings; how it keeps them short is at the end of
[The daily clear](#the-daily-clear)); everything else it edits asks as before,
and the closed files stay closed.
The principal can read these files in [Notes on the web page](#notes-on-the-web-page),
including archived files on request; that view never edits or archives them.

The project's own rules (how it builds, what may never go out, who decides what)
go in the desk's `notes/PROJECT.md`. The card imports it with a line of its own,
`@notes/PROJECT.md` (Claude Code's import syntax for memory files), so Claude Code
loads it with the card at every start, after `/clear` and after compaction, and the
import survives every card refresh. `hansa init` makes the file once, holding only
an HTML comment that says what it is for (Claude Code drops such comments, so it
costs nothing until something is written there); after that hansa rewrites it
only when the coordinator asks, with `hansa notes archive PROJECT.md HEADING`. A
missing file loads nothing and stops nothing. The coordinator and the principal keep it; the
coordinator edits it without asking, as anything under `notes/`. Workers do not
see it: rules for a lane's workers go in its standing instructions. `hansa status` shows its size,
and calls it long past 8 KB: it is part of the conversation's first message, so
every turn rereads it, and 8 KB (some 2,000 tokens) is about the 200 lines Claude
Code advises a memory file to stay under.

The coordinator's session does not load the claude.ai connectors of the Claude
account it logs in with (Claude Code's `disableClaudeAiConnectors`): a connector
is a way to publish outside the machine, and it adds its tools to every turn.
MCP servers set up on the machine stay. hansa also turns off Claude Code's
guessed next message in the coordinator's input box (prompt suggestions); in a
tmux capture without colors that grey guess looked typed. With an older
Claude Code, where a grey line still shows there, the principal should not press
Enter on what looks like an empty box: it may send the guess as if they wrote it.

### Where a desk's files grow

| What | Where | How big |
|---|---|---|
| A task's text, record, report and result | `tasks/<id>/task.md`, `meta.json`, `result.md`, `run.json` | a few KB to some tens of KB each; kept whole |
| What a task's processes printed | `tasks/<id>/run.err` (the worker's errors), `setup.log`, `check.log`, `merge-check.log`, `merge-setup.log`, `merge-check.err` | whatever the lane's setup, check and merge check print: not capped while the task is open (a verbose build can print hundreds of MB) |
| A lane's last try | `lanes/<lane>/try.log`, `merge-check-try.log` | at most 4 MB per step (its first and last 2 MB), replaced by the next try |
| The event log | `events.jsonl` | a few hundred bytes per event, append-only; never trimmed |
| Server output from `hansa up` | `.hansa/serve.log`, `serve.log.1` | the log rotates on the next start when over 20 MB; one backup |
| A release's output | `.hansa/releases/<id>.log` | whatever its command prints; kept |
| A running task's output | `.hansa/runs/<id>/` | moved into `tasks/<id>/` when it ends |
| Each lane's worktree | `worktrees/<lane>/` | the repository's checkout, and what builds leave in it (see [Free space](#free-space)) |
| A worker run's temporary folder | `/tmp/hansa-<id>-...` | what the worker's commands put there; removed when the run ends |
| A lane command's TMPDIR, on Linux | `.hansa/tmp/hansa-check-...` | what a setup's, check's, merge check's or try's build writes there; removed when the command ends |

Once a day `hansa serve` cuts merged tasks' passed setup, check and merge check logs over 289 KB to their
first 32 KB and last 256 KB, with a line counting the bytes cut. Failed logs wait 30 days; other logs of
tasks ended (merged, cancelled, failed, replaced) or left at done that long, and ended releases' logs that
old, keep their last 256 KB, under a line saying when and how big they were. Task cuts appear in the record
and `hansa show`, release cuts only in the event; no log is cut twice. Text, records, reports, results,
the event log and all other logs stay whole.

## Backing up and moving a desk

Back up repositories as ordinary git repositories, branches included. First `hansa pause "backup"`, wait
until `hansa status` shows nothing running, and quit the coordinator's session. There is no `hansa stop`: stop the server
with `x` in `hansa menu`, Ctrl-C, or `kill PID`.

Copy the desk with `cp -a` or `tar -p`: `secrets/` must stay mode 0700, its files 0600, and hansa does not repair them.
Copy everything (settings, `tasks/`, `events.jsonl`, `asks.json`, `notes/`, `lanes/`, `PAUSED`, `.claude/`) except
`worktrees/`, which hansa remakes, and `.hansa/`. From `.hansa/` keep `releases.json`, `releases/`, `attachments/`,
`alerts.json`, `providers.json`, `settings-history.jsonl`, `intent/` and `claude-*/`; hansa rebuilds the rest.
The [intent records](tasks.md#goals-and-product-decisions) keep goals and decisions,
including their reasons, replacements and outcomes.

On the new machine, install the tools, run `hansa doctor`. Logins are per machine: run `claude auth login`. The
coordinator's conversation stays behind. Changed paths: `hansa lane set` cannot change `repo`: edit it in
`settings.json` while stopped, after merging or closing reviewed tasks; check `write`, `env` and `config_dir`. A copied
repository needs `git worktree prune`.

`hansa up` starts the server, lists the desk for this account, and says how to start the coordinator. Run `hansa lane try
L` per lane, then `hansa resume`. Success: `hansa status` and `hansa desks` show your tasks and requests.

## Hearing from hansa

When `hansa serve` starts, it repairs a task end saved just before the server stopped without writing
its event. This includes verifications, task checks and merge checks: the usual follower then posts
the end, and the event list and review alerts can see it. Recovery keeps the saved end time and
fields, and leaves existing end events alone, including on a second restart. A queued task's recorded
cancellation already counts as its end; recovery adds no separate end event. Merge records and their
events share the saved timestamp; recovery also recognizes older merges' rounded event times. To keep
startup quick, it reads the last MiB of the log as it stood before startup, plus events appended since,
and checks saved ends from the hour before that tail's newest event, or later. A stop of any length
does not move this window; startup events cannot move it either. The scan follows the log's byte
order, so an older repaired timestamp cannot hide ends already recorded. It does not recreate
missing task-added events or release ends. This repair is needed while saving an end and appending
its event are separate writes; it can go when both become one durable operation.

hansa posts to the coordinator's Claude Code session through the session's
inbox (a local socket every interactive session listens on) when something
needs the coordinator: a task ended (after hansa's own check), failed, was
cancelled, stopped at its ceiling or could not start; a verification's verdict;
a merge check ended; the principal answered a release; a release ended
(done, or failed with its exit code and last lines) or became void; a provider
an open request names became ready when it was not as the request was made
(once, looked at on a key or settings save, a `hansa ping`, or its rest after a usage
limit running out; the request may be done: if it is, the coordinator closes it
with `hansa ask --close N "<outcome>"`); `hansa serve` stopped, or, still running,
finished no round for 5 minutes (busy or stuck), and then that it runs again;
srt not running; free space went under the floor (`hansa disk-floor`), and then
that it is back over it. A server busy for less than that is not news, and
nothing the coordinator did itself and saw is posted to it.
Time the machine sleeps does not count toward a server being busy or stuck.

Each message is a `[hansa]` header line with the desk, then a bullet line per
event, only the latest for each task or subject; a task's line carries `next:`,
other lines only when one applies; events seconds apart come in one message, and past 10 lines the
rest are counted (`hansa status` has them). A follower does the posting: a small
background process per session and desk, which reads the event log from where it
left off and ends when the session's process does; one that starts after
another ended first posts what was missed meanwhile, as it is now. A `hansa
serve` stop missed meanwhile is told only while the server is still not
running; when it runs again by then, that line is left out, and a busy or stuck
server is left to the follower's own checks. `hansa notify on` returns once its
new follower has its place in the log, not merely once the follower runs, so
an event logged after it returns (a task ending a moment later) is posted; if
the follower has not taken its place within 60 seconds, the command says so and
names the follower's log.

If the session's inbox refuses a post, the follower keeps the lines until a post succeeds. It tries every two
seconds for up to a minute, then gathers new events and tries again with only the latest line per task or subject.
A startup summary also waits until posted before it counts as caught up. If the session's process ends first,
the next follower's catch-up includes what was not posted. While it tries, the follower stays live: `hansa notify`
and `hansa status` still show the session as heard.

Which session hears what: a task or request goes to the session that made it
(`hansa add`, `retry`, `verify`, `merge` and `ask` record it) while
that session has a follower; otherwise to every session hansa posts to, as do
desk-wide events. A cancel or close is not posted to the session whose command
made it. A session started with `hansa coordinator` is posted to by itself;
any other session runs `hansa notify on` once (`hansa notify off` stops it).
`hansa notify` lists the sessions hansa posts to, and `hansa explain notify`
says all of this.

While hansa posts to a session, that session does not keep `hansa wait`
running in the background; `hansa wait` is for a session hansa does not post
to. It waits for tasks only; open to-dos do not keep it waiting. With no tasks
to watch, it returns at once, saying there is nothing to wait for.
Since hansa posts when a task, a verification or a merge ends and when the
principal answers a release, the coordinator needs no scheduled wakeups and no polling to
find out: it ends its turn and waits. Each wakeup or poll is a turn that rereads
the whole conversation, and costs that much even when it finds nothing to do.

These lines come from hansa, not from the principal, and approve nothing. A
session whose Claude Code settings hold or refuse messages from other sessions
(`crossSessionInbound`) does not get them; only the principal can change that.

The principal's `coordinator` alerts cover sessions that cannot hear hansa,
including refused inbox posts, waits on a permission dialog or question,
untouched review work and repeated API errors. The page sets the delays;
`hansa status` shows cases that still hold. The follower can repost unhandled
messages after an API error in a turn hansa started. See [alerts](releases.md#alerts)
for the cases, timing, recovery notices and rules for reposting.

### The principal's to-dos

The coordinator records what only the principal can do with
`hansa ask "<what>" [--why "<why only the principal can do it>"] [--holds]`.
`--holds` says work waits on this item; without it, work goes on. It records that
fact for the principal; it does not pause dispatch. The new-item alert is unchanged.
The principal answers in the conversation. The coordinator closes the item with
`hansa ask --close N "<outcome>"`; an outcome is required. Workers and verifications
cannot close to-dos. Releases still take the principal's yes or no on the page.
Requests answered on the page in an older version stay closed with their answers;
those answers are not posted to the coordinator.

### The daily clear

A coordinator's session can run for days. Each time its context fills, Claude
Code compacts it into a summary; the next compaction summarizes that summary
again together with everything since. The summary keeps growing, the session
starts each stretch heavier, compaction comes sooner, and early detail drifts.
`hansa coordinator --clear-daily HH-HH` (such as `04-09`) clears the session
once a day while it is idle, so it starts again from the desk's state, not from
a summary of summaries. Automatic compaction keeps a full session going; it does
not do what the daily clear does.

It is off by default. hansa hands the window (local time; `22-03` runs past
midnight) to its mod, for that start only, and the mod clears the session as
`/clear` does, once in each window, when the session has been idle for 5
minutes: no turn (hansa's posts and anyone's messages start one) and no
background work waiting. It skips the window the session started in. Inside the window
idle compaction waits for the clear. Without the mod there is no daily clear,
and the start says why.

After any clear, the session is shown `hansa status`, and hansa records when.
`hansa status` and the page show the window and the last clear, and warn once
two windows have ended since without one; the session started as root on an
account's desk records no clear, so there the last clear is not known.

When the session recorded for `hansa up` is one that ran `hansa notify on`
(kind `notify`), `hansa status` and the page instead say in one line that it was
started outside `hansa coordinator`, so the coordinator's mod and the daily
clear are not in it. They do not describe the earlier coordinator's start or
show its daily clear for this session. They also omit the coordinator's compaction
line: neither the mod's idle setting nor the `--autocompact` option passed by
`hansa coordinator` reaches this session through `hansa up`.

So the coordinator keeps what must survive in the desk (tasks, requests, and
its notes under `notes/`), not only in the conversation. It keeps the notes file
it rereads each time it picks the work up short, since each reading costs it:
the current work and open items only. It moves finished rounds to another file
there that it does not reread, without asking the principal and without typing
them out again, with `hansa notes archive FILE HEADING` (to `notes/archive/FILE`
unless `--to` names another); `hansa notes` lists the files with their sizes and
headings. It does not `mv`, `cp` or `sed` them: a shell command there asks.

### Compaction

`hansa coordinator` loads hansa's mod, a Claude Code plugin, into the coordinator's session only (workers,
verifications never get it; a session `hansa up` starts through its launcher gets only its
[status line](#the-status-line)). The principal sets two values on the page, next to the
coordinator's model and effort; the coordinator cannot change them:

- **Compact when idle at** (default 400,000 tokens): once a turn has ended with an answer and the session is idle
  (no turn running, and the last `Stop` listed no background tasks and no session crons), the mod compacts after 60
  seconds with no new turn, while the whole context is still in the prompt cache.
- **Compact while working at** (default 550,000 tokens): Claude Code's own auto-compact, given as `--autocompact`.

Both take 100,000 to 1,000,000 tokens. A new turn cancels the wait; after a compact (done, skipped or refused) the
mod asks again only after a new turn. It reads the first in the desk's settings file at each decision (a turn or a
wait ends): a missing value is its default, a file it cannot read or parse means no idle compact. The second applies
from the next start. The mod needs Claude Code 2.1.306 or later, and `--autocompact` 2.1.290 or later; `--autocompact`
is given whenever Claude Code is new enough, whether or not the mod loads. On a Claude Code older than 2.1.306 the
start says that hansa did not load the coordinator's mod, with the version, 2.1.306 and `claude update`. When hansa skips it, the start, `hansa status` and
the page say why. A session whose settings turn hooks off runs without the mod's rules, and hansa cannot see that;
Claude Code's own auto-compact still applies to it.

The mod's copy is under `.hansa/coordinator-mod/` in the desk, or
`~/.hansa/coordinator-mod/<hash>/` for root on another account's desk.
Every missing folder hansa makes on that path is 0700, as are folders inside
the copy. The follower's own `~/.hansa/followers/<hash>/` uses the same
rule, so starting it first leaves the mod's enclosing folders private.
Existing modes stay unchanged. To keep another account from changing the
code loaded into the session, hansa refuses the mod if its folder or an
enclosing folder belongs to another ordinary account, or is writable by
group or others without the sticky bit. When this account owns a writable
folder, the reason adds `; run chmod go-w <folder>`, with the path shell-quoted;
the start, `hansa status` and the page show that reason. For another account's
folder, the refusal stays as it was. Repair the permissions and start the
coordinator again to load the mod.

Below **compact when idle at**, what compacts an idle session is Claude Code's own idle compaction (Claude Code
2.1.299 or later): a conversation of 200,000 tokens or more left idle about 54 minutes is compacted before its
one-hour prompt cache lapses. `"idleCompaction": false` in Claude Code's settings, or its `/config`, turns it off.
hansa neither sets nor reads it.

### The status line

hansa's mod pins one line of hansa's own under the prompt of the coordinator's session, with Claude Code's
`$.ui.status`, so whoever watches that terminal sees the desk's state at a glance. Only the person at the terminal
sees it: it is not in the conversation, and the model never reads it. It sits beside Claude Code's own pinned notices
and leaves the `statusLine` command in a person's own settings as it is: hansa passes no `statusLine` and changes no
settings file.

```
hansa shop/desk · Opus 5.5 max · waiting for you: releases 1, to-dos 2 · running 2 · queued 1 · review 1 · 24 h $38 · page http://127.0.0.1:8765/
hansa 网店台子 · Opus 5.5 max · 后台没在跑（5 分钟没更新）
```

In order, each item only when it is not zero:

- `hansa` and the desk's name, as alerts give it;
- the session's model, as `/model` shows it, and the effort of its latest model request (before the first, the
  `--effort` the start gave, if any);
- what is wrong: dispatch paused, free space or memory under its floor, the coordinator's turns ending in API
  errors and since when, the last push to an alert channel failed (by service: `bark`, or `command` for the alert
  command), no alert channel set up;
- what waits for the principal: releases waiting for their yes, open to-dos;
- the work: running (merge checks too), queued, waiting for review, failed in the last 24 hours; `idle` when nothing
  runs or waits in the queue;
- the workers' worth in the last 24 hours, as `hansa status` gives it, rounded to dollars;
- the page's address, as `hansa status` gives it, without its key. The key approves releases and the coordinator can
  read its own terminal, so it is never on the line: `hansa web`, run in a terminal of the principal's own, prints
  the full link.

The figures come from `statusline.json` in the desk's folder, which `hansa serve` writes at each round: counts and
short words only, and the time it was written; never a release's one-time code, a known key, a push address, the page's
token, or a task's title or text. See the [redaction limits](releases.md#push-addresses-and-services).
It is outside `.hansa/` and `secrets/`, which the session is denied Read on, so the
mod reads it whether or not Claude Code holds a mod's file reads to those rules; the session may read it (it holds
nothing `hansa status` does not show) but not change it. The mod reads it again every 15 seconds and when a turn
ends. When it is older than two minutes, the line says only that `hansa serve` is not running (or its rounds are
stuck), and for how many minutes it has not written; when it is missing, that `hansa serve` is not running and has
written nothing yet, with no minutes. The words are in the principal's alert language
(`alert_language`, English or Chinese, on the page).

A session `hansa up` starts through its launcher (one recorded with `hansa notify on`) gets the line too, and nothing
else of the mod: no idle compaction and no daily clear, which stay with sessions `hansa coordinator` starts. Its deny
rules and turn hooks are as before.

The line comes with the mod: both need Claude Code 2.1.306 or later, the oldest release checked to have what the
mod's hooks use (an event the engine does not know would keep the whole mod from loading). On an older one neither
`hansa coordinator` nor the launcher loads the mod or prepares a copy of it, and the start says that hansa did not
load the coordinator's mod, why, and to run `claude update`.

## Merge checks

A dedicated merge lane keeps long merge checks off busy task lanes. Checks
still take a concurrency slot and can gather compatible queued merges into a
batch. See [merging](tasks.md#merging-the-merge-check-and-conflicts) for setup,
batch limits, individual checks after a batch fails, and concurrency advice.

`hansa lane set V verify_for x y` reserves V for verifications of the listed lanes; `none` unsets it.
They must share V's repository; none may be V, a merge lane or a verify lane. V cannot be a merge lane.
Ordinary work queued or running prevents setting it. Every settings save checks these rules to prevent
misrouting; unset `verify_for` before giving V ordinary work. Removing a lane removes its list entries;
`--like` copies `verify_for`. Merge lanes' served-lane lists exclude verify lanes, which never merge.

## Free space

Desks run builds and tests many times a day, and a full disk breaks everything on the machine, the desk's own
records among them. So `hansa serve` keeps a floor of free space. Each round it looks at the free space of each
volume it and its runs write on: the desk's folder, each lane's repository and worktree, the folder worker runs get
their temporary folders in (`/tmp`) and its own TMPDIR (where the setups', checks' and tries' are made; on Linux
their own TMPDIR is in the desk's `.hansa/tmp`, on the desk's volume, which is named as a place too). While any
of them has less free than the floor, it starts no new task, verification, try, merge or merge check, or release. What already runs goes on, a running task's check and a resumed worker among it. It says why:

- `hansa status` has a line naming the volume, the places on it, its free space and the floor (`DISK LOW: ...`),
  and always one with the free space of the desk's volume and the floor, also when nothing is under it;
- the page shows the same in its status line, and the floor next to the free space under Settings, Lanes, General;
- `hansa explain server` lists each volume with its free space;
- a queued task, merge or release says it waits for space;
- the coordinator's session is told once when a volume goes under the floor and once when all are back over it,
  and every alert channel set up for the principal receives one disk alert (the command gets `HANSA_ALERT_KIND=disk`).

It starts again by itself once every volume has the floor and 1 GB more free, so that it does not stop and start
at every round around the line. A release asked meanwhile waits, approved, and goes once there is space.

If a write fails anyway (a full disk, for example), `hansa serve` goes on, and still stops runs that are
cancelled or over their time limit. Free space: once writes work again, it records the ends it kept, a run
stopped at its limit as timed out. After a restart, a check's end is found without running the check again;
a worker's exit code that could not be recorded stays unknown, as before.

If serve restarts before it can save a failed start's record, ordinary recovery applies: the stopped process is gone, workers and verifications return to the queue, checks and merge checks run again, releases are recorded as lost and tries end.

If a write fails just after git has merged, set work aside or committed, hansa's record or message may not
show what git did: git is right, and `hansa git L log` shows it.

The floor holds for each volume by its own size. A volume smaller than four times the floor has a quarter of its
size as its floor: on a Linux server that mounts `/tmp` as a tmpfs (systemd's default size is half the memory), an
8 GB machine has a 3.9 GB `/tmp`, which under the default floor of 10 GB holds back only under about 1 GB free,
not always. The margin it needs before `hansa serve` starts again is scaled with it, to no more than a quarter of
that volume's floor (about 0.1 GB there). A volume of four times the floor or more (40 GB under the default) keeps
the floor as set, and so does a volume whose total size is not known. Where a volume's floor is not the setting,
the `DISK LOW` line, what a queued task, merge or release says, `hansa explain server` and the page give the one
that applies to it (`floor 1.0 GB for this 3.9 GB volume`), and say how much each volume needs before it starts
again.

Each place is looked at on its own. One that cannot be looked at (a lane's repository in a folder the account
cannot read, say) is left out, and only it: the others are measured and hold as before, and `hansa status`, the
page and `hansa explain server` say which place cannot be looked at, since when and why, and that the free space
of its volume is not known and holds nothing back. If the look fails as a whole, `hansa serve` holds nothing back
while it fails, and `hansa status` says so with what the last look that worked measured and when, rather than a
`DISK LOW` that no longer holds. Either is an event (`disk_look_failed`, and `disk_look_ok` once it can look again)
when it changes, not each round, and the coordinator's session hears it.

The floor is the coordinator's, as concurrency is: `hansa disk-floor` shows it with the free space of the desk's
volume, `hansa disk-floor 20` sets it to 20 GB (default 10; 0 turns it off for every volume). It is one number:
a small volume's own floor follows from it, as above. The page shows it and cannot change it.

Memory has a floor too, guarding against starting more work while memory runs low. `hansa memory-floor [GB]`
shows or sets it (coordinator only; default 2; 0 disables). Machines smaller than four times the floor use a
quarter of their memory. Each round reads macOS `kern.memorystatus_level` percent of `hw.memsize`, or Linux
`MemAvailable`. The same starts wait; running work continues. Starts resume at the floor plus 1 GB.
Status shows available memory, the floor and `MEMORY LOW:`; explain server and waiting tasks say why.
`memory_low`/`memory_ok` notify the coordinator once, without principal alerts. Failed readings hold nothing
back, recorded and shown like disk failures; `memory_look_failed`/`memory_look_ok` report changes only.

Free space rather than lower the floor. A lane whose builds leave large outputs in its worktree (`target/`,
`build/`, `node_modules/`, a compiler's cache), or fill a cache of its own (a Maven or pip cache on its write
list), should clean them in its setup, which runs before each task: `mvn -q clean` or `rm -rf build`, or a cache
pruned past a size, keeps the worktree from growing with every task.
