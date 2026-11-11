"""Codes for the page, alongside the original English used by commands.

Task, provider and coordinator records predate these codes and keep their English
reasons. Decode only known, complete forms at the view boundary; never guess at
an unfamiliar sentence. This also lets an older page show a newer server's text.
"""
from __future__ import annotations

import re


def message(code: str, text: str, **values) -> dict:
    return {"code": code, "values": values, "text": text}


EXACT = {
    "claude was not found; install Claude Code or set HANSA_CLAUDE": "claude_missing",
    "hansa could not tell which Claude Code this is from claude --version": "mod_version_unknown",
    "not in settings": "missing_provider", "no endpoint address": "no_endpoint", "no key": "no_key",
    "credit balance too low": "credit_low", "no answer": "no_answer",
    "not logged in (Please run /login)": "not_logged_in",
    "cancelled": "cancelled", "cancelled before it started": "cancelled_before_start",
    "hansa serve stopped as it started the release, so whether its command ran is not known: read its output (hansa release), and ask again if it has to go out": "release_lost",
    "it was interrupted before it could end (no exit code)": "release_interrupted",
}
# Persisted reasons: full matches keep paths, identifiers and program output intact.
PATTERNS = [
    ("mod_copy", r"copying the mod into (?P<path>.+) failed \((?P<said>.*)\)"),
    ("mod_mkdir", r"making the mod's folder (?P<path>.+) failed \((?P<said>.*)\)"),
    ("mod_home", r"finding the folder for the mod's copy \(the home folder\) failed \((?P<said>.*)\)"),
    ("mod_version", r"asking Claude Code its version \(claude --version\) failed \((?P<said>.*)\)"),
    ("repository_missing", r"its repository folder (?P<path>.+) is gone \(moved or removed\)"),
    ("worktree_failed", r"git cannot prepare its worktree (?P<path>.+?): (?P<said>.*)"),
    ("setup_failed", r"the lane's setup command failed \(exit (?P<exit>\d+)\), so the worker never started; hansa show (?P<task>t\d+) has the setup's last lines"),
    ("task_ceiling", r"stopped at the ceiling of (?P<count>\d+) (?P<unit>minutes|turns): the task was probably too big or unclear, or the worker got stuck"),
    ("no_output", r"no output \(exit (?P<exit>-?\d+)\)"),
    ("mod_old", r"Claude Code (?P<version>[\d.]+) is older than (?P<needs>[\d.]+)"),
    ("mod_folder", r"its copy would be in a folder another account could change: (?P<path>.+)"),
    ("serve_code", r"hansa serve runs (?P<runs>.+), but (?P<build>.+) was (?P<installed>.+): restart it"),
    ("serve_started", r"hansa serve started (?P<started>.+), before (?P<build>.+) was (?P<installed>.+): restart it"),
    ("srt_missing", r"srt MISSING \((?P<where>.+)\): hansa needs srt .+Install it with `(?P<install>[^`]+)` with Node (?P<node>[^ ]+) or newer on that PATH .+"),
    ("srt_failed", r"srt CANNOT RUN \(as hansa serve saw it(?P<when>.*?)\): it is at (?P<path>.+), but a sandboxed `true` failed: (?P<said>.*)\. Setups and checks do not run until this is fixed \(hansa explain server\)"),
    ("alert_failed", r"alert command FAILED (?P<at>.+?) \((?P<how>exit .*?|timed out after .*? s)\)(?:: (?P<said>.*))? · the principal's, on the page"),
    ("coordinator_absent", r"no session hears from hansa, so the coordinator's session cannot be reached: (?P<count>\d+) thing\(s\) wait for it, the oldest for (?P<minutes>\d+) min \((?P<oldest>.*)\)"),
    ("coordinator_inbox_gone", r"the coordinator's session (?P<who>.+) cannot be reached: its inbox (?P<path>.+) is gone \(no such file\) for (?P<minutes>\d+) min, as after a restart of the session in place \(Claude Code's /restart gives it a new inbox\); hansa keeps the lines · next: hansa notify on, from that session"),
    ("coordinator_inbox", r"the coordinator's session (?P<who>.+) cannot be reached: its inbox refuses posts for (?P<minutes>\d+) min; hansa keeps the lines and tries again"),
    ("coordinator_window", r"the coordinator's session (?P<who>.+) has waited on its window for (?P<minutes>\d+) min \(a permission dialog or a question\), with what hansa posted to it unanswered"),
    ("coordinator_turn", r"the coordinator's session (?P<who>.+): its turns end with an API error \((?P<error>[^)]+)\) since (?P<at>[\d:]+) \((?P<minutes>\d+) min\) · hansa posted again what waits (?P<reposts>\d+) time\(s\) · (?P<told>.+)"),
    ("coordinator_principal", r"the coordinator's session (?P<who>.+): the principal's own request ended with an API error \((?P<error>[^)]+)\) at (?P<at>[\d:]+) and was not done · (?P<advice>.+) · (?P<told>.+)"),
    ("coordinator_review", r"waiting for review while the coordinator's session (?P<who>.+) is idle: (?P<tasks>.+)"),
    ("provider_quota", r"out of quota \((?P<said>.*)\)(?:: (?P<detail>.*))?"),
    ("provider_key", r"key rejected \((?P<said>.*)\)(?:: (?P<detail>.*))?"),
    ("provider_timeout", r"no answer in (?P<seconds>[\d.]+)s"),
    ("provider_turn_limit", r"(?P<models>.*): answered, but stopped at the turn limit"),
    ("provider_overloaded", r"the provider was overloaded \((?P<said>.*)\)"),
    ("provider_server", r"the provider had a server error \((?P<said>.*)\)"),
    ("provider_network", r"the connection to the provider failed \((?P<said>.*)\)"),
    ("attachment_missing", r"its attachment (?P<path>.+) is gone"),
    ("release_limit", r"stopped at (?P<whose>the lane's|target (?P<target>.+)'s) time limit \((?P<minutes>[\d.]+) min\)"),
]
PREFIXES = {
    "error: ": "program_error",
    "could not start: ": "task_start", "the lane's setup could not run: ": "setup_start",
    "the sandbox could not start: ": "sandbox_start", "it could not start: ": "release_start",
}


def describe(text: str) -> dict:
    text = text or ""
    if text in EXACT:
        return message(EXACT[text], text)
    for prefix, code in PREFIXES.items():
        if text.startswith(prefix):
            return message(code, text, reason=describe(text[len(prefix):]))
    for code, pattern in PATTERNS:
        if match := re.fullmatch(pattern, text, re.DOTALL):
            values = match.groupdict()
            if code == "serve_code" or code == "serve_started":
                # installed_text's English label is presentation, not part of a version or date.
                values["installed"] = values["installed"].removeprefix("installed ")
                values["runs"] = values.get("runs", "").replace("(installed ", "(")
            if code == "alert_failed":
                values["withheld"] = values["said"] == "its output was not kept, as the alert held a one-time code"
                values["timeout"] = values["how"].startswith("timed out")
                values["number"] = values["how"].removeprefix("timed out after ").removesuffix(" s") if values["timeout"] else values["how"].removeprefix("exit ")
            if code in ("coordinator_turn", "coordinator_principal"):
                from .alert import principal_turn_text
                values["told_at"] = values["told"].removeprefix("the principal was alerted at ") if values["told"].startswith("the principal was alerted at ") else ""
                values["alerts_off"] = "alerts are off" in values["told"]
                if code == "coordinator_principal":
                    values["principal_zh"] = principal_turn_text(values["error"], values["at"], zh=True)
            if code == "coordinator_review":
                values["tasks"] = [dict(task=m[1], minutes=m[2]) for m in re.finditer(r"(t\d+) for (\d+) min", values["tasks"])]
            return message(code, text, **values)
    return message("unknown", text)


def annotate(record: dict, fields=("error", "why")) -> dict:
    return {**record, **{key + "_message": describe(record[key]) for key in fields if record.get(key)}}


def provider(text: str, state: dict, name: str) -> dict:
    """The rest's timestamps and policy are already recorded, so do not parse its sentence."""
    import time
    values = {"provider": name, "reason": describe(state.get("reason", ""))}
    if state.get("credit_since"):
        return message("provider_credit", text, since=state["credit_since"], shared=bool(state.get("shared_rest")))
    simple = describe(text)
    if simple["code"] != "unknown" or not text:
        return simple
    if state.get("cool_until", 0) > time.time():
        # Login and key remedies include the exact command (and account) chosen by the server.
        if "; saving a new key brings it back" in text:
            return message("provider_save_key", text, **values)
        match = re.fullmatch(r".*; log in with (?P<login>.+), then `hansa ping .+` brings it back", text)
        if match:
            login = match["login"]
            account = re.fullmatch(r"(?P<command>.+) as (?P<account>.+) \(from root: (?P<root>.+)\)", login)
            return message("provider_login", text, login=account["command"] if account else login,
                           account=account["account"] if account else "", root=account["root"] if account else "", **values)
        return message("provider_rest", text, until=state["cool_until"], how=state.get("rest_how"),
                       minutes=state.get("rest_minutes"), **values)
    if state.get("unreachable_since"):
        check = state.get("check") or {}
        waiting = re.search(r"; (\d+) tasks? waits? for it$", text)
        return message("provider_unreachable", text, since=state["unreachable_since"],
                       reason=describe(state.get("unreachable_reason", "")), check=annotate(check, ("detail",)),
                       waiting=int(waiting[1]) if waiting else None)
    return simple


def place(text: str) -> dict:
    known = {"the desk's folder": "place_desk", "the runs' temporary folders": "place_temporary",
             "hansa serve's TMPDIR": "place_serve_tmp", "lane commands' TMPDIR": "place_lane_tmp"}
    if text in known:
        return message(known[text], text)
    if m := re.fullmatch(r"lane (.+)'s (repository|worktree)", text):
        return message("place_lane", text, lane=m[1], kind=m[2])
    return message("unknown", text)


def status(desk, s: dict, st: dict) -> dict:
    from . import disk, memory
    for key in ("serve_code", "srt_missing", "alert_failed"):
        st[key + "_message"] = describe(st[key])
    st["unreached_messages"] = [describe(x) for x in st["unreached"]]
    if st["mod"]:
        st["mod"] = annotate(st["mod"])
    for row in st["lanes"]:
        if row.get("held"):
            row["held"] = {k: annotate(v) for k, v in row["held"].items()}
    st["failed"] = [annotate(row) for row in st["failed"]]
    states = desk.provider_state()
    for row in st["providers"]:
        row["problem_message"] = provider(row["problem"], states.get(row["name"], {}), row["name"])
        if row["check"]:
            row["check"] = annotate(row["check"], ("detail",))
    low = st["disk"]["low"]
    if low:
        fl = disk.floor(s)
        st["disk"]["low_message"] = message("disk_low", st["disk"]["low_text"], volumes=[
            {"mount": v.get("mount"), "free": disk.gb(v.get("free") or 0),
             "floor": disk.floor_text(disk.vol_floor(fl, v)), "again": disk.gb(disk.vol_bar(fl, v)),
             "places": [place(x) for x in v.get("what", [])], "total": disk.gb(v["total"]) if v.get("total") else None} for v in low])
    if st["memory"]["low"]:
        rec = memory.record(desk)
        fl = memory.machine_floor(memory.floor(s), rec)
        st["memory"]["low_message"] = message("memory_low", st["memory"]["low_text"], available=disk.gb(rec["available"]),
                                              floor=disk.floor_text(fl), again=disk.gb((fl + memory.MARGIN) * disk.GB))
    return st


def release_problem_zh(text: str) -> str:
    """Release rows also supply what_zh to alerts; keep their existing bilingual interface."""
    r = describe(text)
    v = r["values"]
    return {"release_start": lambda: f"无法启动，程序报告：“{v['reason']['text']}”；请修复后重新请求发布并答复",
            "release_lost": lambda: "hansa serve 在发布启动时停止，无法确认命令是否运行；请运行 hansa up，查看输出（hansa release），需要时重新请求并答复",
            "release_interrupted": lambda: "发布中断，没有退出码；请确认服务器运行（hansa up），检查发布结果后重新请求并答复",
            "release_limit": lambda: (f"达到发布目标 {v['target']} 的运行时限" if v.get("target") else "达到工作线的运行时限") + f"（{v['minutes']} 分钟）；请检查发布结果和时限后重新请求并答复",
            }.get(r["code"], lambda: text)()
