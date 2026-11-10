"""Alerts: push addresses and the principal's own alert command.

To add a push address on the page:
1. Open Settings, Alerts, Push addresses, then Add an address.
2. Paste the whole address from your push app or service. Leave the service at Recognize it, or choose its name.
   Each service's hint on the page says what to paste; an extra secret field appears only when it takes one.
3. Optionally name it, then press Save settings. The saved entry shows only a hidden address.
4. Press Send a test push on that entry. Its last result says sent with the time, or explains what went wrong.
For Bark, install and open the Bark app on your iPhone, then paste the whole address shown on its home screen,
such as https://api.day.app/KEY/example, into Add an address. Hansa keeps the server and key and drops the example
text. Choose Bark explicitly for a self-hosted address if Recognize it asks you to choose a service.
For encryption, choose AES-128, AES-192 or AES-256 with mode CBC in Bark's encryption settings. Paste the same
16, 24 or 32 character ASCII key, or the app's copied example, into Encryption key (optional) on the entry.
Only the key is saved; the example must match this device and CBC key length. Save settings, then test.
The entry says encrypted on the page, in hansa status and hansa explain. A phone showing Decryption Failed
has a different key. Each push uses a fresh random IV; every notification field is encrypted.
There may be up to ten entries. Each has an on/off switch and an optional name. To replace an address or extra
secret, paste a new one into that entry; leaving either empty keeps the saved value. Switch, rename or Remove,
then Save settings. A test uses the saved entry even when it is off. Unsaved addresses and secrets stay only
in this page until saved or reloaded.
Only the principal's page save changes addresses. Each whole address, including its key, stays in a 0600 file
under the desk's 0700 secrets/ folder, with the same protection for an encryption key, signing secret or access token.
Settings hold only metadata; history records changes without credentials. Page replies and status hide secret
parts. Previously saved Bark settings and key move automatically to the first push address, keeping on or off;
the old setting and key file are removed and settings history records the move.

Each gathered batch goes to every enabled entry with an address, even with no alert command. The desk's name
and alert texts leave this machine for each address's service; for phone apps they go on to Apple's or Google's
push service. Those services can read the text unless Bark encryption is enabled: then Bark's server and Apple
see ciphertext and routing information, without the desk name or alert text.
One-time release codes go to the alert command and to Bark entries that encrypt. Other push entries get
code-free wording. The principal may always answer on the page, or give the code to the coordinator in the
conversation. The sends run side by side with independent
deadlines and results, so one slow or failed destination does not delay another's send or change its result.
Hansa status shows each service, hidden address, on/off switch and last result (sent or its reason).

The principal sets one command line on the page (settings `alert`, which only the page saves: desk.PRINCIPAL_ONLY);
the page records that it saved it, and when. Beside it, `alert_language` selects "en" (the default) or "zh".
Before that choice has been saved, the page prefills it from its own language; only saving applies it to alerts.
Only the principal may save it, and the settings history records the change. Other values are refused on save;
an invalid value in settings.json reads as "en". A saved change also applies to alerts still gathering.
A settings save that omits alert_language keeps the saved choice; if none was saved, it stays unset.
hansa runs the command when a release starts waiting for the principal's yes,
when a release ends (done, failed, void or forbidden), when the coordinator stops one (release_stopped; the principal's
own Stop on the page sends none), when a to-do for the principal is recorded (hansa ask), when a lane's runs cannot
start because a provider ran out of credit (once per rest: run.rest_provider), when a lane's runs cannot
start because their sandbox cannot (once for each lane and reason until a passing try clears the hold: run.hold),
when free space goes under the floor
and hansa serve starts nothing new (once each time: disk.look),
when hansa serve has finished no round for STUCK_ALERT seconds, and when what hansa posts to the coordinator's session
cannot reach it (kind `coordinator`, below); not for ordinary task events, which go to the
coordinator. The coordinator sends none: no hansa command does.
After an alerted disk, sandbox or server_stuck problem clears, one passive recovery line joins the next batch,
with the same kind and when the trouble began and ended. A sandbox recovery is per lane. The beginning is kept
across processes and restarts in alert-recovery.json; a later sandbox hold alerts again even for the same reason.

The `coordinator` alert has four cases, each after the minutes the principal sets on the page (settings
`coordinator_alert`, desk.COORDINATOR_ALERT; 0 turns a case off):
    no session listens   something a follower would post (notify._item) has waited no_session_min in the event log
                         while the desk has no live follower: hansa serve sends it once (unheard_check), and again only
                         after a follower has run and the case begun anew; a running session's inbox refusing posts
                         for those minutes also alerts once, until a post succeeds
    waits on its window  `claude agents --json` lists a live follower's session as `waiting` (a permission dialog or a
                         question) window_min after something was posted to it: once per stretch of waiting
    work sits untouched  a task posted to the session has waited review_min for review, and the session is `idle` at
                         a lookup after that: once for that task; hansa does not post it again
    its turns fail       API errors keep ending the session's turns (its Stop and StopFailure hooks' record): after
                         turn_min (default 30), or at once for one only the principal can cure (case `turn`, urgent);
                         once a turn ends normally after that alert, case `turn_ok` (passive)
The follower watches its inbox's refusals and the last three (notify.Watch, notify.Turns), and asks the desk to queue
them (from_follower).

When an API error ends a turn hansa started (something was posted between the turn before's end and the failure),
the follower first posts again what was posted to the session and not yet handled (a turn that ended normally
handled what came before it), headed "Your last turn ended with an API error (ERROR) at HH:MM", and only while the
session is seen idle after the failure: at most three reposts in a stretch of failures, one per failed turn, none
once the principal was alerted. By the error:
    rate_limit, overloaded, server_error, unknown: waiting may cure them; reposts at 2, 10 and 30 minutes after the
        stretch began, and the alert after turn_min; for rate_limit it says a usage limit may only reset hours later
    billing_error, oauth_org_not_allowed, account_on_hold, verification_required, model_not_found,
    cloud_credential_error, invalid_request: only the principal can cure them; the alert goes at once, and no repost
        until a turn ends normally (also with turn_min 0, which only stops the alert); for invalid_request it says
        /compact or /clear in that session may cure it
    authentication_failed (perhaps a short login refresh): one repost after 2 minutes, and the alert if the turn
        after it fails too, or after 2 minutes when nothing waits; it says to log in again in that session
    max_output_tokens: one repost at once, ending "go on"; the alert if the next failure is that too
A turn the principal started is never posted again; it alerts after turn_min. The alert names the error, since when,
and how many things posted to the session wait unhandled. turn_min 0 turns the `turn` and `turn_ok` alerts off;
the reposts still go. hansa status shows a line while a stretch holds for a session hansa posts to: the error, since
when, how many reposts went and whether the principal was alerted; none once a turn ended normally.

Alerts are queued only while a channel is set up, in .hansa/alerts.json (any hansa process may queue one), and sent
by hansa serve's alert thread. Gathering, one run at a time and the hourly limit apply to the whole batch.
The alert command runs:
as the desk's account, outside the sandbox, in a folder of its own under the desk's secrets/alert-runs (run_dir),
with hansa's own environment (not a lane's env),
the kinds in HANSA_ALERT_KIND (comma-separated when several go out together), the level in HANSA_ALERT_LEVEL,
and the text on standard input (UTF-8,
keys removed, within MOST_TEXT characters), stopped after TIMEOUT seconds. Long batches shorten each alert
separately, urgent ones first, retaining each start and the total count. Alerts that come within GATHER seconds
of each other go out in one run (none waits more than MOST_WAIT); closed to-dos and release requests that no longer
wait as asked are left out, while their end alerts still go out.
Each queued alert has a level: passive for a release that finished successfully and recovery notices;
active for release_waiting, ask,
a failed, void or forbidden release, release_stopped and test; urgent for server_stuck, coordinator, sandbox, disk
and credit. A batch takes the highest (passive < active < urgent); Bark maps that level to its own attention levels.
One run at a time (.hansa/alert.lock), and at
most MOST_RUNS in an hour: beyond that, what comes waits and goes out in one run when the hour frees a run, with a
line saying how many were held back. An entry of the queue that cannot be read is dropped, and recorded once
(event alert_dropped); the others go. The command's own exit ends its run, even when it leaves a child behind;
only at the time limit is its process group killed. A run
that fails or times out is recorded (event alert_failed with channel command, and .hansa/alert-last.json,
which hansa status and the page read until the next successful command run); the command is never retried. Each push entry records its last result in
.hansa/alert-push-ID-last.json, and alert_failed with channel push, id and service on failure, never its address.
Bark has its own 30-second deadline,
including alert_http's at most three tries; the alert run adds no retries. A
release_waiting alert queues only its release ID, request time and text without a code. Just before sending,
the text is rebuilt in memory with a new code (release.new_code), only while that request still waits
and an alert command or enabled Bark entry that encrypts gets the batch. Make it once per release before the
channels start, sharing the same code across eligible channels; other push entries get the gathered text without
codes. If making a code fails, void the codes already made, record alert_error and send the whole batch
without codes, asking for an answer on the page. With neither eligible channel, no code is made.
A code went out when any channel carrying it sent;
when no channel's final text held it, including after character or byte cuts, it is void. Neither
the code, what the command printed for such an alert, nor a code-bearing push's response text is written to a
file (only how it ended is kept). Removing a Bark encryption key makes later batches to that entry code-free.
An entry's test push never makes or carries a release code. A phone
shows the first hundred characters or so, so the code and what to do with it come right after the desk's name:
"[hansa] desk NAME: release #1 (lane a, target staging) · code K7P3QX: to release it, give this code to the coordinator
in the conversation (this release only, once, 24 hours; send it nowhere else), or answer on the page · main at
08e2cfc99f, 8 commit(s), command: ...; note: ..."; several in one run: "[hansa] desk NAME: 3 alerts · #1 code K7P3QX
· #2 code M4R8TW", then one line each, urgent alerts first, then other lines holding a code.
That folder is removed when the command ends, and what a stopped
or killed hansa serve left there is swept when it starts (sweep_runs): secrets/ is closed to the coordinator, workers,
verifiers, so a leftover that holds the alert's text is never theirs to read. Text is in the saved alert_language;
English wording is unchanged when there is no target description. A release alert puts its target's description,
kept as it was when asked, after the request, code and instructions, or the result.
"""
from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import without_session
from .desk import Desk, SettingsError, desk_name_problem, now, read_json, write_json

KINDS = ("release_waiting", "release_ended", "release_stopped", "ask", "server_stuck", "sandbox", "disk", "coordinator", "credit",
         "test")
LEVELS = ("passive", "active", "urgent")  # increasing attention; a batch takes the highest
URGENT = ("server_stuck", "coordinator", "sandbox", "disk", "credit")
GATHER = 20  # seconds: alerts that come within this of each other go out in one run
MOST_WAIT = 120  # seconds an alert waits at most for others to gather with it
TIMEOUT = 30  # seconds a run may take
MOST_TEXT = 1000  # characters on the command's standard input
TAIL = 5  # lines of a failed run's output kept
WITHHELD = "its output was not kept, as the alert held a one-time code"  # nor any part of it, in any form
MOST_RUNS = 20  # runs an hour at most; beyond it, alerts are held and go out together
HOUR = 3600
STUCK_ALERT = 15 * 60  # seconds hansa serve finishes no round before the principal is alerted
EVERY = 2.0  # seconds between the alert thread's looks
UNHEARD_EVERY = 10.0  # seconds between its looks at whether any session hears from hansa (unheard_check)


def queue_file(desk: Desk) -> Path:
    return desk.state_dir / "alerts.json"


def last_file(desk: Desk) -> Path:
    return desk.state_dir / "alert-last.json"


def runs_dir(desk: Desk) -> Path:
    """Where each run's own folder is made: in secrets/, which the coordinator, workers and verifiers
    may not read, as what the command writes there may hold the alert's text and its code."""
    return desk.secrets_dir / "alert-runs"


def sweep_runs(desk: Desk) -> None:
    """Remove the folders of runs that a stopped or killed hansa serve left (hansa serve calls this as it starts,
    before its alert thread runs)."""
    d = runs_dir(desk)
    if d.is_dir():
        for x in d.iterdir():
            if x.is_dir() and not x.is_symlink():
                shutil.rmtree(x, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    x.unlink()


def command(s: dict) -> str:
    """The principal's alert command line; '' when none is set."""
    a = s.get("alert")
    return a["command"].strip() if isinstance(a, dict) and isinstance(a.get("command"), str) else ""


def configured(desk: Desk, s: dict | None = None) -> bool:
    """Whether the command or any enabled push address can take alerts."""
    from . import alert_push
    s = desk.settings() if s is None else s
    return bool(command(s) or alert_push.enabled(desk, s))


def release_codes(desk: Desk, s: dict | None = None) -> bool:
    """Whether a configured channel may carry a waiting release's code."""
    from . import alert_push
    s = desk.settings() if s is None else s
    return bool(command(s) or any(alert_push.encrypts(desk, e) for e in alert_push.enabled(desk, s)))


def chinese(desk: Desk) -> bool:
    return desk.settings().get("alert_language") == "zh"


def level_of(kind: str) -> str:
    return "urgent" if kind in URGENT else "active"


def _good(x) -> bool:
    at = x.get("at") if isinstance(x, dict) else None
    return (isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at)
            and isinstance(x.get("kind"), str) and isinstance(x.get("text"), str))


def _read(desk: Desk) -> tuple:
    """The queue's entries that can be read, and how many cannot (a hand edit: hansa writes it whole)."""
    try:
        raw = queue_file(desk).read_bytes()
    except FileNotFoundError:
        return [], 0
    try:  # text that is not UTF-8 is read with replacement characters: one bad byte loses no alert
        q = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return [], 1 if raw.strip() else 0
    if not isinstance(q, list):
        return [], 1
    good = [x for x in q if _good(x)]
    return good, len(q) - len(good)


def queued(desk: Desk) -> list:
    with desk.lock():
        return _clean(desk)


def _clean(desk: Desk) -> list:
    """Under the desk lock: the queue's good entries, with any that cannot be read dropped and recorded once."""
    from . import release
    good, bad = _read(desk)
    migrated = False
    for x in good:
        if x.get("level") not in LEVELS:
            x["level"] = level_of(x["kind"])
            migrated = True
        if "code" not in x:
            continue
        # Older queues stored the credential and its text. Remove both, even during a rate-limit hold;
        # revoke only that code, so migrating an old entry cannot revoke a newer, already delivered one.
        code = x.pop("code")
        rid = x.get("release")
        if isinstance(code, str) and code:
            release.code_unsent(desk, rid, code)
        r = release.get(desk, rid) or {}
        x["asked"] = r.get("asked")
        x["text"] = desk.redact(_release_text(desk, rid, True) or f"release #{rid} waits for your yes on the page")
        x.pop("translations", None)
        migrated = True
    if bad or migrated:
        write_json(queue_file(desk), good)
    if bad:
        desk.event("alert_dropped", entries=bad, why="an entry of .hansa/alerts.json that cannot be read")
    return good


def add(desk: Desk, kind: str, text: str, release=None, asked=None, ask=None, *, zh: str | None = None,
        level: str | None = None) -> bool:
    """Queue code-free text for the next run; False when no alert channel is set up. release and asked identify
    the waiting request whose text is rebuilt with a code at send time. ask: the to-do's ID, so a closed
    to-do can be left out when the gathered alerts go out. Keep both code-free wordings, so a saved language
    change also applies to alerts still gathering; only _with_codes adds a credential, in memory."""
    if not configured(desk):
        return False
    with desk.lock():
        q = _clean(desk)
        if release is not None:
            q = [x for x in q if x.get("release") != release]
        translations = {"en": desk.redact(text), "zh": desk.redact(zh)} if zh is not None else {}
        q.append({"kind": kind, "text": translations.get("zh" if chinese(desk) else "en", desk.redact(text)),
                  "level": level if level in LEVELS else level_of(kind), "at": time.time(),
                  **({"translations": translations} if translations else {}),
                  **({"release": release, "asked": asked} if release is not None else {}),
                  **({"ask": ask} if ask is not None else {})})
        desk.state_dir.mkdir(mode=0o700, exist_ok=True)
        write_json(queue_file(desk), q)
    return True


# -- what is alerted -------------------------------------------------------------------------------------------------
def _problem(desk: Desk, kind: str, text: str, *, zh: str, lane: str = "", since: float | None = None) -> None:
    """Remember the beginning only when its alert is queued, for one passive all-clear after recovery.
    Keep it on disk: holds and their recovery may be recorded by different processes or across a restart."""
    with desk.lock():
        if add(desk, kind, text, zh=zh):
            path = desk.state_dir / "alert-recovery.json"
            rec = read_json(path, {}) or {}
            rec.setdefault(f"{kind}:{lane}", time.time() if since is None else since)
            write_json(path, rec)


def _recovered(desk: Desk, kind: str, text: str, *, zh: str, lane: str = "", at: float | None = None) -> None:
    """End an alerted stretch once. An explicit round time must advance beyond the stalled round."""
    with desk.lock():
        path = desk.state_dir / "alert-recovery.json"
        rec = read_json(path, {}) or {}
        key = f"{kind}:{lane}"
        since = rec.get(key)
        if since is None or at is not None and at <= since:
            return
        end = time.time() if at is None else at
        began = time.strftime("%Y-%m-%d %H:%M", time.localtime(since))
        ended = time.strftime("%Y-%m-%d %H:%M", time.localtime(end))
        add(desk, kind, f"{text}; trouble lasted from {began} to {ended}",
            zh=f"{zh}；此前故障从 {began} 持续到 {ended}", level="passive")
        rec.pop(key)
        write_json(path, rec)


def _release_text(desk: Desk, rid, waiting: bool, code: str = "", zh: bool = False) -> str:
    from . import release
    r = release.get(desk, rid)
    if not r:
        return ""
    about = (" · " + r["about"]) if r.get("about") else ""
    target = r.get("target") or ""  # "": the lane's one unnamed target, its release command
    if zh:
        at = f"{r.get('base')} 的 {str(r.get('commit') or '')[:10]}"
        rest = (f"{r.get('count', 0)} 个提交，命令：{r.get('command')}"
                + (f"；说明：{r['note']}" if r.get("note") else ""))
        if waiting and r.get("direct_count"):
            rest = f"直接提交：{r['direct_count']} 个，" + rest
        if waiting and code:
            return (f"发布 #{r['id']}（工作线 {r.get('lane')}，发布目标 {target or '（未命名）'}）· 一次性码 {code}："
                    f"要发布，请在对话里把这个码告诉协调员（仅限这次发布，只能用一次，{release.CODE_LIFE // 3600} 小时内有效；"
                    f"不要发到别处），或在网页上点头{about} · {at}，{rest}")
        head = f"工作线 {r.get('lane')} 的发布 #{r['id']}，发布目标 {target or '（未命名）'}（{at}）"
        return (f"{head} 等你在网页上点头：{rest}{about}" if waiting else
                f"{head}：{release.state_text(desk, r, zh=True)}{about}")
    at = f"{r.get('base')} at {str(r.get('commit') or '')[:10]}"
    rest = (f"{r.get('count', 0)} commit(s), command: {r.get('command')}"
            + (f"; note: {r['note']}" if r.get("note") else ""))
    if waiting and r.get("direct_count"):
        rest = f"{r['direct_count']} direct commit(s), " + rest
    if waiting and code:  # the code and what to do with it first: a phone shows the first hundred characters or so,
        # and no cut of a long text takes it
        return (f"release #{r['id']} (lane {r.get('lane')}, target {target or '(unnamed)'}) · code {code}: to release "
                f"it, give this code to the coordinator in the conversation (this release only, once, "
                f"{release.CODE_LIFE // 3600} hours; send it nowhere else), or answer on the page{about} · {at}, {rest}")
    head = f"release #{r['id']} of lane {r.get('lane')}, target {target or '(unnamed)'} ({at})"
    if waiting:
        return f"{head} waits for your yes on the page: {rest}{about}"
    return f"{head}: {release.state_text(desk, r)}{about}"


def from_event(desk: Desk, kind: str, fields: dict) -> None:
    """Queue the alert an event calls for, if any (Desk.event calls this)."""
    if kind.startswith("release_"):
        from . import release
        r = release.get(desk, fields.get("id")) or {}
        if r.get("after_merge") and not (kind == "release_ended" and r.get("state") == "failed"):
            return  # these automatic checks alert the principal only on failure
    if kind == "release_asked" and fields.get("state") == "waiting" or kind == "release_waits":
        from . import release
        rid = fields.get("id")
        if not configured(desk):
            return
        with desk.lock():
            r = release.get(desk, rid) or {}
            add(desk, "release_waiting", _release_text(desk, rid, True), release=rid, asked=r.get("asked"),
                zh=_release_text(desk, rid, True, zh=True))
    elif kind in ("release_ended", "release_void", "release_forbidden"):
        add(desk, "release_ended", _release_text(desk, fields.get("id"), False),
            zh=_release_text(desk, fields.get("id"), False, zh=True),
            level="passive" if r.get("state") == "done" else "active")
    elif kind == "release_stopped" and fields.get("by") != "page":  # the principal knows of their own Stop
        add(desk, "release_stopped", _release_text(desk, fields.get("id"), False),
            zh=_release_text(desk, fields.get("id"), False, zh=True))
    elif kind == "ask":
        a = next((x for x in desk.asks() if x.get("id") == fields.get("id")), {})
        add(desk, "ask", f"to-do #{fields.get('id')} for you: {a.get('text', '')}", ask=fields.get("id"),
            zh=f"你的待办 #{fields.get('id')}：{a.get('text', '')}")
    elif kind == "provider_credit":
        at = time.strftime('%H:%M', time.localtime(fields.get("since") or time.time()))
        add(desk, "credit", fields["text"],
            zh=f"服务商 {fields.get('provider')}：余额不足，自 {at} 起休息，等你在网页上确认充值")
    elif kind == "sandbox_held" and fields.get("alert"):  # once for each lane and reason until cleared (run.hold)
        from .run import sandbox_held_text
        held = {**fields, "kind": fields.get("run")}
        why = sandbox_held_text(held)
        _problem(desk, "sandbox", f"lane {fields.get('lane')} cannot run: {why}; "
                             + (f"task {fields['task']} is held" if fields.get("task") else "its tasks are held")
                             + ", not failed",
            zh=f"工作线 {fields.get('lane')} 因沙箱暂缓：{sandbox_held_text(held, zh=True)}；"
               + (f"任务 {fields['task']} 暂缓" if fields.get("task") else "其任务暂缓") + "，没有失败",
            lane=fields.get("lane", ""))
    elif kind == "sandbox_ready":
        _recovered(desk, "sandbox", f"lane {fields.get('lane')}'s sandbox works again",
                   zh=f"工作线 {fields.get('lane')} 的沙箱已恢复", lane=fields.get("lane", ""))
    elif kind == "disk_low":  # once each time it goes under (disk.look)
        _problem(desk, "disk", f"free space is under the floor ({fields.get('floor') or 0:g} GB): "
                          + "; ".join(fields.get("volumes") or []) + ": hansa serve starts no new task or release "
                          "until there is space again; what runs goes on",
            zh=f"磁盘空间低于下限（{fields.get('floor') or 0:g} GB）：" + "；".join(fields.get("volumes") or [])
               + "：hansa serve 暂停新任务和发布，等待空间恢复；正在运行的继续")
    elif kind == "disk_ok":
        _recovered(desk, "disk", "free space is back over the floor: hansa serve starts things again",
                   zh="磁盘空间已恢复到下限以上：hansa serve 恢复启动新工作")


# -- sending -----------------------------------------------------------------------------------------------------------
def desk_name(desk: Desk) -> str:
    """The saved display name, or the last two folders. Read without migrating settings: desk lists only read."""
    try:
        name = desk._saved_settings().get("desk_name", "")
        if not desk_name_problem(name) and not desk.keys_in_desk_name(name) and name.strip():
            return name.strip()
    except (SettingsError, OSError, UnicodeError):
        pass  # An unavailable or hand-edited setting must not break desk lists or put controls in headings.
    return "/".join(str(desk.root).split("/")[-2:]).lstrip("/") or "/"


def text_of(desk: Desk, items: list, held: int = 0) -> str:
    """What the command reads: keys removed, urgent alerts first, each line sharing MOST_TEXT characters.
    held: how many of them the limit of MOST_RUNS runs an hour held back."""
    items = sorted(items, key=lambda x: (x.get("level", level_of(x.get("kind"))) != "urgent",
                                        not x.get("code")))
    lines = [desk.redact(" ".join(str(x.get("text") or "").split())) for x in items]
    codes = [x for x in items if x.get("code")]
    zh = chinese(desk)
    name = desk_name(desk)
    head = f"[hansa] 工作台 {name}" if zh else f"[hansa] desk {name}"
    held_text = ((f"（每小时最多 {MOST_RUNS} 次，{held} 条提醒因此暂缓）" if zh else
                  f" ({held} held back by the limit of {MOST_RUNS} runs an hour)") if held else "")
    if len(lines) == 1:  # a code stays early: what was held back is said after it
        text = f"{head}: {lines[0]}{held_text}" if codes else f"{head}{held_text}: {lines[0]}"
    else:  # the first line gives each waiting release's number and code, before what was held back
        text = ((f"{head}: {len(lines)} 条提醒" if zh else f"{head}: {len(lines)} alerts")
                + "".join(f" · #{x.get('release')} {'一次性码' if zh else 'code'} {x['code']}" for x in codes)
                + held_text + "\n" + "\n".join(f"- {x}" for x in lines))
    text = desk.redact(text)
    if len(text) <= MOST_TEXT:
        return text
    if len(lines) > 1:
        # Reserve a share for every alert before shortening any line. Small alerts use only what they
        # need; the remaining space is shared by longer ones. Keep this while a batch has one text cap.
        header = text.split("\n", 1)[0]
        budget = max(0, MOST_TEXT - len(header) - 3 * len(lines))  # newline and "- " per line
        sizes = [0] * len(lines)
        for remaining, i in zip(range(len(lines), 0, -1), sorted(range(len(lines)), key=lambda i: len(lines[i]))):
            sizes[i] = min(len(lines[i]), budget // remaining)
            budget -= sizes[i]
        text = header + "".join("\n- " + (line if len(line) <= size else line[:max(0, size - 1)] + "…" if size else "")
                                for line, size in zip(lines, sizes))
    return text if len(text) <= MOST_TEXT else text[:MOST_TEXT - 1] + "…"


def run(desk: Desk, cmd: str, kinds: str, text: str, timeout: float = TIMEOUT, codes=(), level: str = "active") -> dict:
    """Run the alert command once: {ok, exit, timed_out, limit, tail, at}. It runs in a folder of its own under
    runs_dir, removed after however the command ends (what a killed server left, sweep_runs removes), so that what it
    writes to a relative path lands nowhere hansa keeps or anyone but the principal reads. codes: the one-time codes the text holds;
    then none of what the command printed is kept (tail is empty, withheld says why), as it may echo a code in any
    form."""
    env = {**without_session(os.environ), "HANSA_ALERT_KIND": kinds, "HANSA_ALERT_LEVEL": level}
    for k in ("HANSA_TASK", "HANSA_MERGING"):
        env.pop(k, None)
    timed_out, code, out = False, None, b""
    # stdin is a pipe so a code never lands in a temporary file. Output goes straight to /dev/null for
    # code-bearing runs, otherwise to a file: descendants holding output open cannot delay the command's exit.
    desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(desk.secrets_dir, 0o700)
    runs_dir(desk).mkdir(mode=0o700, exist_ok=True)
    with contextlib.ExitStack() as stack:
        here = stack.enter_context(tempfile.TemporaryDirectory(prefix="run-", dir=runs_dir(desk)))
        said = None if codes else stack.enter_context(tempfile.TemporaryFile())
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=here, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL if codes else said,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            stack.enter_context(proc.stdin)
            try:
                proc.communicate(text.encode("utf-8"), timeout=timeout)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    proc.wait(timeout=5)
            if said is not None:
                said.seek(0)
                out = said.read()
        except OSError as e:
            out = str(e).encode()
    result = {"ok": code == 0 and not timed_out, "exit": code, "timed_out": timed_out, "limit": timeout, "tail": [],
              "at": now(),
              "kinds": kinds}
    if codes:
        return {**result, "withheld": WITHHELD}
    said = desk.redact(out.decode("utf-8", errors="replace")).strip().splitlines()[-TAIL:]
    return {**result, "tail": [x[:200] for x in said]}


@contextlib.contextmanager
def _one_run(desk: Desk, wait: bool):
    """One alert run at a time, across processes; yields False when another holds it and wait is False."""
    desk.state_dir.mkdir(mode=0o700, exist_ok=True)
    with open(desk.state_dir / "alert.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            got = True
        except BlockingIOError:
            got = False
        try:
            yield got
        finally:
            if got:
                fcntl.flock(f, fcntl.LOCK_UN)


def _last(desk: Desk) -> dict:
    try:
        rec = read_json(last_file(desk), {})
    except ValueError:  # not UTF-8: a hand edit; hansa writes it whole
        rec = {}
    return rec if isinstance(rec, dict) else {}


def _runs(rec: dict, t: float) -> list:
    """When the runs of the last hour began."""
    runs = rec.get("runs")
    return [x for x in runs if isinstance(x, (int, float)) and t - HOUR < x <= t] if isinstance(runs, list) else []


def _pending(desk: Desk, queue: list) -> list:
    """Leave out asks and release requests answered since they were queued."""
    from . import release
    closed = {a["id"] for a in desk.asks() if "id" in a and a.get("closed")}
    pending = []
    for x in queue:
        if x["kind"] == "ask" and x.get("ask") in closed:
            continue
        if x["kind"] == "release_waiting":
            r = release.get(desk, x.get("release")) or {}
            if r.get("state") != "waiting" or not x.get("asked") or r.get("asked") != x["asked"]:
                continue
        pending.append(x)
    return pending


def hold_status(desk: Desk) -> dict | None:
    """The current hourly hold, counting only alerts still awaiting delivery."""
    with desk.lock():
        rec = _last(desk)
        runs = sorted(_runs(rec, time.time()))
        if not rec.get("held_since") or len(runs) < MOST_RUNS:
            return None
        count = len(_pending(desk, _read(desk)[0]))
        return {"count": count, "until": runs[-MOST_RUNS] + HOUR} if count else None


def _record(desk: Desk, result: dict, count: int) -> None:
    last = _last(desk)
    rec = {**last, "last": result, "failed": None if result["ok"] else result}
    write_json(last_file(desk), rec)
    if result["ok"]:
        desk.event("alert_sent", kinds=result["kinds"], alerts=count)
    else:
        desk.event("alert_failed", channel="command", kinds=result["kinds"], alerts=count, exit=result["exit"],
                   timed_out=result["timed_out"], limit=result.get("limit"), tail=result["tail"],
                   **({"withheld": result["withheld"]} if result.get("withheld") else {}))


def send_due(desk: Desk, gather: float = GATHER, most_wait: float = MOST_WAIT, timeout: float = TIMEOUT,
             wait: bool = False, capped: bool = True) -> dict | None:
    """Send what is queued once it has gathered: when no alert came for `gather` seconds, or the first has waited
    `most_wait`; and, when capped, only if fewer than MOST_RUNS runs began in the last hour (else they wait, and go
    out together when the hour frees a run). Return the command's result when set, otherwise the first push's,
    plus every result in channels; None when nothing went out. Each channel records independently."""
    with _one_run(desk, wait) as mine:
        if not mine:
            return None
        with desk.lock():
            good = _clean(desk)
        if not good:
            return None
        t = time.time()
        if t - max(x["at"] for x in good) < gather and t - min(x["at"] for x in good) < most_wait:
            return None
        rec = _last(desk)
        if capped and len(_runs(rec, t)) >= MOST_RUNS:
            if not rec.get("held_since"):  # recorded once per hold
                write_json(last_file(desk), {**rec, "held_since": t})
                desk.event("alert_held", alerts=len(good), most_runs=MOST_RUNS)
            return None
        with desk.lock():
            q = _pending(desk, _clean(desk))
            write_json(queue_file(desk), [])
        s = desk.settings()
        from . import alert_push
        cmd, pushes = command(s), alert_push.enabled(desk, s)
        held_since = rec.get("held_since") if isinstance(rec.get("held_since"), (int, float)) else None
        if held_since:
            write_json(last_file(desk), {k: v for k, v in rec.items() if k != "held_since"})
        if not (cmd or pushes) or not q:  # cleared since they were queued: nothing to send them with
            return None
        # A page test bypasses the hold; only its queued companions were held.
        held = sum(x["kind"] != "test" for x in q) if held_since else 0
        kinds = ",".join(dict.fromkeys(x["kind"] for x in q))
        level = max((x["level"] for x in q), key=LEVELS.index)
        lang = "zh" if chinese(desk) else "en"
        for x in q:
            translations = x.get("translations")
            if isinstance(translations, dict) and isinstance(translations.get(lang), str):
                x["text"] = translations[lang]
        # Count the batch once, including push-only batches. Each channel records
        # its own result as soon as it finishes, without waiting for its sibling.
        write_json(last_file(desk), {**{k: v for k, v in rec.items() if k != "held_since"},
                                     "runs": _runs(rec, t) + [t]})
        items = [dict(x) for x in q]
        try:
            codes = _with_codes(desk, items) if cmd or any(alert_push.encrypts(desk, e) for e in pushes) else []
        except Exception as e:
            # Code preparation must not take the gathered batch away from every channel.
            # Use the original page-only wording, including when an earlier code was made.
            made = [x for x in items if x.get("code")]
            for x in made:
                with contextlib.suppress(Exception):
                    _codes_went(desk, [x], [])
            from . import release
            with contextlib.suppress(Exception):
                desk.event("alert_error", error=release.hide_codes(str(e), [x["code"] for x in made])[:300])
            items, codes = [dict(x) for x in q], []
        deliveries = []  # in memory only: each channel's final text and whether it sent

        def send_command():
            text = ""
            try:
                text = text_of(desk, items, held)
                result = run(desk, cmd, kinds, text, timeout, codes=[x["code"] for x in codes], level=level)
            except Exception:  # setup failures also belong only to this channel; never log a possible code
                result = {"ok": False, "exit": None, "timed_out": False, "limit": timeout,
                          "tail": [], "at": now(), "kinds": kinds}
            deliveries.append((text, result["ok"]))
            _record(desk, result, len(items))
            return result

        def send_push(entry):
            batch = items if alert_push.encrypts(desk, entry) else q
            why = alert_push.send(desk, entry, batch, held, deliveries=deliveries)
            last = alert_push.last_result(desk, entry["id"]) or {}
            return {"channel": "push", "id": entry["id"], "service": entry["service"],
                    "name": desk.redact(entry.get("name", "")), "ok": why == "sent", "why": why,
                    **({"code": last["code"]} if "code" in last else {})}

        with ThreadPoolExecutor(max_workers=len(pushes) + bool(cmd), thread_name_prefix="hansa-alert-channel") as pool:
            push_runs = [pool.submit(send_push, entry) for entry in pushes]
            command_run = pool.submit(send_command) if cmd else None
            channels = ([{"channel": "command", **command_run.result()}] if command_run else [])
            channels.extend(run.result() for run in push_runs)
        _codes_went(desk, codes, deliveries)
        return {**channels[0], "channels": channels}


def _with_codes(desk: Desk, items: list) -> list:
    """Add codes only in memory, once per release in this run, under the lock that protects its state.
    Requests get increasing IDs, including re-asks after a void; asked also binds the queued text to the request.
    Keep this check while queued alerts may outlive the request they describe."""
    from . import release
    codes = []
    seen = set()
    with desk.lock():
        for x in items:
            rid = x.get("release")
            if x["kind"] != "release_waiting" or not isinstance(rid, int) or rid in seen:
                continue
            r = release.get(desk, rid) or {}
            if r.get("state") != "waiting" or not x.get("asked") or r.get("asked") != x["asked"]:
                continue
            code = release.new_code(desk, rid)
            if code:
                seen.add(rid)
                x["code"] = code
                codes.append(x)
                x["text"] = _release_text(desk, rid, True, code, zh=chinese(desk))
    return codes


def _codes_went(desk: Desk, codes: list, deliveries: list) -> None:
    """A code went out if any channel carrying it sent; void it only when no channel's final text held it."""
    from . import release
    for x in codes:
        carried = [ok for text, ok in deliveries if x["code"] in text]
        if carried:
            release.code_sent(desk, x.get("release"), x["code"], any(carried))
        else:
            release.code_unsent(desk, x.get("release"), x["code"])


def test(desk: Desk, timeout: float = TIMEOUT) -> dict:
    """The page's test button: a test alert sent now, with anything already queued."""
    if not configured(desk):
        return {"ok": False, "problems": ["no alert channel is set up; save a command or enable a push address first"]}
    cmd = command(desk.settings())
    add(desk, "test", "a test alert from the page: the alert command works" if cmd else "a test alert from the page",
        zh="来自网页的测试提醒：提醒命令正常" if cmd else "来自网页的测试提醒")
    result = send_due(desk, gather=0, timeout=timeout, wait=True, capped=False)  # the principal's own press
    if result:
        return {**result, "ok": all(channel["ok"] for channel in result["channels"])}
    return {"ok": False, "problems": ["the test alert was not sent"]}


def stuck_check(desk: Desk, told: float) -> float:
    """hansa serve's rounds quiet for STUCK_ALERT seconds: alert once, until a round comes again. told: when that
    was alerted, 0 if not; returns it as it is now."""
    from .run import _last_round, _server_elapsed
    last, mono = _last_round(desk)
    quiet = _server_elapsed(last, mono) if last else 0
    if quiet < STUCK_ALERT:
        heartbeat = read_json(desk.state_dir / "heartbeat")
        at = heartbeat.get("at") if isinstance(heartbeat, dict) else heartbeat
        if last and at == last:  # serve.lock's start time alone does not mean a round finished
            _recovered(desk, "server_stuck", "hansa serve is finishing rounds again",
                       zh="后台（hansa serve）已恢复完成轮次", at=last)
        return 0.0
    if not told:
        _problem(desk, "server_stuck", f"hansa serve has finished no round for {int(quiet // 60)} minutes: no task starts "
                                  "or ends, and no release runs; see hansa explain server",
            zh=f"后台（hansa serve）已 {int(quiet // 60)} 分钟没跑完一轮：在忙或卡住了；"
               "任务无法开始或结束，发布也无法运行；请查看 hansa explain server", since=last)
        return time.time()
    return told


# -- the coordinator's session cannot be reached (kind `coordinator`) -------------------------------------------------
def unheard_file(desk: Desk) -> Path:
    return desk.state_dir / "coordinator-alert.json"


def _unheard(desk: Desk) -> dict:
    try:
        rec = read_json(unheard_file(desk), {})
    except ValueError:  # not UTF-8: a hand edit; hansa writes it whole
        rec = {}
    return rec if isinstance(rec, dict) else {}


def _mins(seconds: float) -> int:
    return max(0, int(seconds // 60))


def _at(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _short(line: str) -> str:
    """What waits, as a follower's line says it, without its next command."""
    line = " ".join(str(line).split()).split(" · next: ")[0]
    return line if len(line) <= 160 else line[:159] + "…"


def _waiting(rec: dict) -> list:
    """What the record says waits unheard, the oldest first: [{at, line}]."""
    waits = rec.get("waits") if isinstance(rec.get("waits"), dict) else {}
    return sorted((x for x in waits.values() if isinstance(x, dict) and _at(x.get("at"))), key=lambda x: x["at"])


def unheard_check(desk: Desk, t: float | None = None) -> dict:
    """hansa serve's look at the first case of the `coordinator` alert: something a follower would post (notify._item)
    has waited no_session_min in the event log, and the desk has no live follower. What waits since a follower last
    posted is kept in unheard_file ({pos, waits: {what: {at, line}}, told}); told: when it was alerted. A live
    follower, or the case turned off, clears it: the case begins anew. So does a follower the register shows started
    after the alert, live or not (one may start and end between two looks): what waits is then read again from where
    it got to. Returns the record as it is now."""
    from . import notify
    from .desk import coordinator_alert_settings
    t = time.time() if t is None else t
    s = desk.settings()
    mins = coordinator_alert_settings(s)["no_session_min"]
    rec = stored = _unheard(desk)
    end, recs = notify.log_end(desk), notify.records(desk)
    if not mins or not isinstance(rec.get("pos"), int) or any(notify.is_live(r) for r in recs):
        new = {"pos": end}  # a session hears from hansa (or this is the first look): nothing waits unheard
    else:
        if _at(rec.get("told")) and any(_at(r.get("since")) and r["since"] > rec["told"] for r in recs):
            rec = {"pos": rec["pos"]}  # a follower ran since the alert: the case begins anew
        posted = [r["pos"] for r in recs if isinstance(r.get("pos"), int)]  # where a follower got to
        pos = min(max([rec["pos"], *posted]), end)
        waits = dict(rec["waits"]) if isinstance(rec.get("waits"), dict) else {}
        while pos < end:
            events, at = notify.read_log(desk, pos, end - pos)
            if at == pos:
                break
            pos = at
            for e in events:
                it = notify._item(desk, e)
                if it and not it[4]:  # not a cancel the session made itself: that one knows
                    key = f"task {it[0][1]}" if it[0][0] in ("task", "merge") else " ".join(map(str, it[0]))
                    waits.setdefault(key, {"at": e["ts"] if _at(e.get("ts")) else t,
                                           "line": _short(desk.redact(it[1]))})
        new = {"pos": pos, "waits": waits, **({"told": rec["told"]} if _at(rec.get("told")) else {})}
        items = _waiting(new)
        if items and "told" not in new and t - items[0]["at"] >= mins * 60 and configured(desk, s) \
                and add(desk, "coordinator", unheard_text(desk, items, t), zh=unheard_text(desk, items, t, zh=True)):
            new["told"] = t
    if new != stored:
        desk.state_dir.mkdir(mode=0o700, exist_ok=True)
        write_json(unheard_file(desk), new)
    return new


def unheard_text(desk: Desk, items: list, t: float, zh: bool = False) -> str:
    """The first case's alert: the case, the session that last heard from hansa, and what waits."""
    from . import notify
    last = max(notify.records(desk), key=lambda r: r.get("seen") or 0, default=None)
    if last:
        sess = last.get("session") or {}
        if zh:
            how = "会话已结束" if notify.gone(sess) else "会话仍在运行，但提醒已关闭或跟随进程已停止"
            who = (f"上次是会话 {notify.who(sess.get('pid'), str(last.get('name') or '尚未命名'))}，"
                   f"直到 {_when(last.get('ended') or last.get('seen'))}：{how}")
        else:
            how = ("its session ended" if notify.gone(sess) else
                   "its session runs, but notifications were turned off or its follower stopped")
            who = (f"the last was session {notify.who(sess.get('pid'), str(last.get('name') or ''))}, until "
                   f"{_when(last.get('ended') or last.get('seen'))}: {how}")
    else:
        who = "这个工作台还没有会话接收过" if zh else "none has on this desk yet"
    if zh:
        return (f"协调员未收到：没有会话接收 hansa 的消息（{who}）· {len(items)} 件事在等协调员，"
                f"最早的已等 {_mins(t - items[0]['at'])} 分钟：{items[0]['line']}")
    return (f"the coordinator's session cannot be reached: no session hears from hansa ({who}) · {len(items)} "
            f"thing(s) wait for the coordinator, the oldest for {_mins(t - items[0]['at'])} min: {items[0]['line']}")


def session_minutes(desk: Desk) -> dict:
    """What a follower watches its session for (notify.Watch): inbox refusals and the second and third cases;
    0 for off, all 0 when no alert channel is set up, so that it looks nothing up."""
    from .desk import coordinator_alert_settings
    s = desk.settings()
    mins = coordinator_alert_settings(s)
    on = configured(desk, s)
    return {"no_session": mins["no_session_min"] if on else 0,
            "window": mins["window_min"] if on else 0, "review": mins["review_min"] if on else 0,
            "turn": mins["turn_min"] if on else 0}


def reviews_file(desk: Desk) -> Path:
    return desk.state_dir / "coordinator-review.json"


def from_follower(desk: Desk, me: dict, req: dict) -> str:
    """A follower's case of the `coordinator` alert (notify.Watch), queued in words: the session (its pid, and its name
    when known), the case, and what waits. The third case goes once per task's round across the desk, however many
    followers watch it (a task that records no session is every follower's): reviews_file keeps {task: its round's
    finished} for those alerted that still wait for review. Returns "queued", "told" (another follower's alert went
    for it), or "" when nothing was queued."""
    from . import notify
    sess = me.get("session") if isinstance(me.get("session"), dict) else {}
    who = notify.who(sess.get("pid"), str(me.get("name") or "")[:80])
    who_zh = notify.who(sess.get("pid"), str(me.get("name") or "尚未命名")[:80])
    t = time.time()
    if req.get("case") == "inbox":
        since = req["since"] if _at(req.get("since")) else t
        text = (f"the coordinator's session {who} cannot be reached: its inbox refuses posts for "
                f"{_mins(t - since)} min · hansa keeps the lines and tries again")
        text_zh = (f"协调员未收到：会话 {who_zh} 的收件箱已拒收 {_mins(t - since)} 分钟 · hansa 保留消息并重试")
    elif req.get("case") == "window":
        since = req["since"] if _at(req.get("since")) else t
        count = req["count"] if isinstance(req.get("count"), int) else 0
        text = (f"the coordinator's session {who} has waited on its window (a permission dialog or a question) for "
                f"{_mins(t - since)} min, so it cannot act on what hansa posted to it: {count} thing(s) since "
                f"{time.strftime('%H:%M', time.localtime(since))}, the oldest: {_short(req.get('oldest') or '')} · "
                "answer it in that session")
        text_zh = (f"协调员的会话 {who_zh} 已在窗口上等了 {_mins(t - since)} 分钟（权限对话框或提问），"
                   f"无法处理 hansa 发来的消息：从 {time.strftime('%H:%M', time.localtime(since))} 起共 {count} 件，"
                   f"最早的是：{_short(req.get('oldest') or '')} · 请在那个会话里回答")
    elif req.get("case") == "review" and notify.TASK_ID.match(str(req.get("task") or "")):
        tid = req["task"]
        try:
            m = desk.meta(tid)
        except SystemExit:
            return ""
        title = m.get("title", "")
        posted = req["posted"] if _at(req.get("posted")) else t
        text = (f"the coordinator's session {who} is idle while {tid} has waited for review for {_mins(t - posted)} "
                f"min since hansa posted it there: {title} · hansa does not post it again")
        text_zh = (f"协调员的会话 {who_zh} 空闲，但 {tid} 自 hansa 发到那里后已等了 {_mins(t - posted)} 分钟待审："
                   f"{title} · hansa 不会重复发送")
        with desk.lock():
            try:
                told = read_json(reviews_file(desk), {})
            except ValueError:  # not UTF-8: a hand edit; hansa writes it whole
                told = {}
            told = {k: v for k, v in (told.items() if isinstance(told, dict) else ())
                    if notify.waits_review(desk, k)}  # merged, closed or queued again: forgotten
            round_ = m.get("finished")
            if tid in told and told[tid] == round_:
                return "told"
            queued = add(desk, "coordinator", text, zh=text_zh)
            if queued:
                told[tid] = round_
            desk.state_dir.mkdir(mode=0o700, exist_ok=True)
            write_json(reviews_file(desk), told)
        return "queued" if queued else ""
    elif req.get("case") == "turn":
        text, text_zh = turn_text(req, who, who_zh, t)
    elif req.get("case") == "turn_ok":
        since = req["since"] if _at(req.get("since")) else t
        at = req["at"] if _at(req.get("at")) else t
        text = (f"the coordinator's session {who} works again: a turn ended normally at "
                f"{time.strftime('%H:%M', time.localtime(at))}, after API errors since "
                f"{time.strftime('%H:%M', time.localtime(since))}")
        text_zh = (f"协调员的会话 {who_zh} 恢复工作：{time.strftime('%H:%M', time.localtime(at))} 有一轮正常结束，"
                   f"此前自 {time.strftime('%H:%M', time.localtime(since))} 起 API 一直出错")
        return "queued" if add(desk, "coordinator", text, zh=text_zh, level="passive") else ""
    else:
        return ""
    return "queued" if add(desk, "coordinator", text, zh=text_zh) else ""


TURN_ADVICE = {  # what the principal may do, by the error (up.TURN_ERRORS) that keeps ending the coordinator's turns
    "rate_limit": ("a usage limit may only reset hours later", "用量上限可能要几个小时后才重置"),
    "invalid_request": ("the session's context may have gone wrong: /compact or /clear in that session may cure it",
                        "会话的上下文可能出了问题：在那个会话里 /compact 或 /clear 也许能解决"),
    "authentication_failed": ("log in again in that session", "请在那个会话里重新登录"),
}


def turn_text(req: dict, who: str, who_zh: str, t: float) -> tuple:
    """The `turn` case's alert (notify.Turns), (English, Chinese): the error, since when, how many things wait."""
    since = req["since"] if _at(req.get("since")) else t
    count = req["count"] if isinstance(req.get("count"), int) and not isinstance(req.get("count"), bool) else 0
    error = str(req.get("error") or "unknown")[:40]
    at = time.strftime("%H:%M", time.localtime(since))
    en, zh = TURN_ADVICE.get(error, ("", ""))
    text = (f"the coordinator's session {who} cannot work: its turns end with an API error ({error}) since {at} "
            f"({_mins(t - since)} min) · {count} thing(s) hansa posted to it wait unhandled" + (f" · {en}" if en else ""))
    text_zh = (f"协调员的会话 {who_zh} 无法工作：自 {at} 起（{_mins(t - since)} 分钟）轮次一直以 API 错误（{error}）结束 · "
               f"hansa 发去的 {count} 件事尚未处理" + (f" · {zh}" if zh else ""))
    return text, text_zh


def coordinator_lines(desk: Desk, s: dict, t: float | None = None) -> list:
    """For hansa status: one line for each case of the `coordinator` alert that holds now, alerted or not (the second
    and third as the followers last recorded them; they look only while an alert channel is set up), and one for
    each session whose turns keep ending in API errors (notify.Turns), alert or no alert."""
    from . import notify
    from .desk import coordinator_alert_settings
    t = time.time() if t is None else t
    mins = coordinator_alert_settings(s)
    lives, out = notify.live_records(desk), []
    turn_on = bool(mins["turn_min"]) and configured(desk, s)
    items = _waiting(_unheard(desk)) if not lives and mins["no_session_min"] else []
    if items and t - items[0]["at"] >= mins["no_session_min"] * 60:
        out.append(f"no session hears from hansa, so the coordinator's session cannot be reached: {len(items)} "
                   f"thing(s) wait for it, the oldest for {_mins(t - items[0]['at'])} min ({items[0]['line']})")
    for r in lives:
        holds = r.get("coordinator") if isinstance(r.get("coordinator"), dict) else {}
        who = notify.who((r.get("session") or {}).get("pid"), r.get("name") or "")
        since = holds.get("inbox")
        if mins["no_session_min"] and _at(since) and t - since >= mins["no_session_min"] * 60:
            out.append(f"the coordinator's session {who} cannot be reached: its inbox refuses posts for "
                       f"{_mins(t - since)} min; hansa keeps the lines and tries again")
        since = holds.get("window")
        if mins["window_min"] and _at(since) and t - since >= mins["window_min"] * 60:
            out.append(f"the coordinator's session {who} has waited on its window for {_mins(t - since)} min (a "
                       "permission dialog or a question), with what hansa posted to it unanswered")
        tasks = holds.get("review") if isinstance(holds.get("review"), dict) else {}
        shown = [f"{tid} for {_mins(t - at)} min" for tid, at in sorted(tasks.items()) if _at(at)]
        if mins["review_min"] and shown:
            out.append(f"waiting for review while the coordinator's session {who} is idle: {', '.join(shown)}")
        if line := turn_line(holds.get("turn"), who, t, turn_on):
            out.append(line)
    return out


def turn_line(turn, who: str, t: float, on: bool) -> str:
    """For hansa status: the stretch of failed turns a follower last recorded for its session (notify.Turns.holds),
    or '' when none holds. on: whether a turn alert can go (an alert channel set up, and turn_min not 0)."""
    turn = turn if isinstance(turn, dict) else {}
    since = turn.get("since")
    if not _at(since) or not since:
        return ""
    reposts = turn["reposts"] if isinstance(turn.get("reposts"), int) and not isinstance(turn["reposts"], bool) else 0
    told = turn.get("told")
    told = ("the principal was alerted at " + time.strftime("%H:%M", time.localtime(told)) if _at(told) and told
            else "the principal is not alerted yet" if on
            else "the principal is not alerted: turn alerts are off (no alert channel, or turn_min 0)")
    error = str(turn.get("error") or "unknown")[:40]
    return (f"the coordinator's session {who}: its turns end with an API error ({error})"
            f" since {time.strftime('%H:%M', time.localtime(since))} ({_mins(t - since)} min) · hansa posted again "
            f"what waits {reposts} time(s) · {told}")


def start_thread(desk: Desk, every: float = EVERY) -> threading.Thread:
    """hansa serve's alert thread: it sends what gathered, and alerts when the server's own rounds stop coming or
    no session hears from hansa (unheard_check, every UNHEARD_EVERY seconds)."""
    def loop():
        told, unheard = 0.0, 0.0
        while True:
            try:
                told = stuck_check(desk, told)
                if time.time() >= unheard:
                    unheard = time.time() + UNHEARD_EVERY
                    unheard_check(desk)
                send_due(desk)
            except Exception as e:  # noqa: BLE001 - keep sending; the error is on record
                with contextlib.suppress(Exception):
                    desk.event("alert_error", error=str(e)[:300])
            time.sleep(every)
    t = threading.Thread(target=loop, name="hansa-alert", daemon=True)
    t.start()
    return t


# -- words -------------------------------------------------------------------------------------------------------------
def _when(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"


def failure_line(desk: Desk) -> str:
    """For status and the page: a failed command run, until a later run sends successfully."""
    rec = _last(desk)
    f = rec.get("failed")
    if not isinstance(f, dict):
        return ""
    last = rec.get("last") or {}
    if last.get("ok") and (last.get("at") or 0) >= (f.get("at") or 0):
        return ""  # also clear warnings in records saved before successful runs cleared "failed"

    limit = f.get("limit") if isinstance(f.get("limit"), (int, float)) and not isinstance(f.get("limit"), bool) \
        else TIMEOUT  # recorded before the limit was: the default
    how = f"timed out after {limit:g} s" if f.get("timed_out") else f"exit {f.get('exit')}"
    tail = " | ".join(f.get("tail") or []) or (WITHHELD if f.get("withheld") else "")
    return (f"alert command FAILED {_when(f.get('at'))} ({how})" + (f": {tail}" if tail else "")
            + " · the principal's, on the page")


def explain_line(s: dict, desk: Desk | None = None) -> str:
    """For hansa explain: which channels the principal set up (the alert command and push addresses), and what the command is."""
    cmd = command(s)
    from . import alert_push, alert_services
    destinations = alert_push.public(desk, s) if desk else []
    pushes = ("Push addresses: " + "; ".join(
        f"{alert_services.SERVICES[e['service']].names['en']} {e['name'] or e['id']} ({e['address']}; "
        f"{'on' if e['on'] else 'off'}{'; encrypted' if e.get('encrypted') else ''})"
        for e in destinations) if destinations else "No push addresses are set up")
    a = s.get("alert") if isinstance(s.get("alert"), dict) else {}
    code_channels = ("One-time codes go to the alert command and to Bark entries that encrypt. "
                     "Other push entries get code-free wording. The principal answers on the page, or says the code "
                     "in the conversation, and you pass it on with hansa release yes ID CODE (or no ID CODE). "
                     "Never guess one.")
    if not cmd:
        return (f"Alerts: no alert command is set. {pushes}. The principal sets these up on the page, Settings, Alerts; hansa "
                "alerts through those set up when a release waits for the principal or ends, a request is filed, and "
                "the rest hansa help alerts lists; you send no alerts and cannot set them up. "
                + (code_channels if desk and release_codes(desk, s) else
                   "With no alert command or enabled Bark entry that encrypts, no release gets a one-time code: "
                   "the principal approves releases on the page only."))
    return (f"Alerts: the principal's alert command is {cmd!r} (saved on the page {_when(a.get('at'))}); hansa runs it "
            "when a release waits for the principal or ends, you stop a release (hansa release --stop), a request is "
            "filed (hansa ask), a lane's runs cannot start because their sandbox cannot, free space goes under the "
            "floor (hansa disk-floor), hansa serve finishes no round for a long time, or what hansa posts to your "
            "session cannot reach it (no session hears from hansa, its inbox refuses posts, your session waits on a "
            "permission dialog or a question, or a task posted to it waits for review while it is idle). You send no "
            f"alerts and cannot set it. {pushes} (set up on the page, Settings, Alerts). " + code_channels)
