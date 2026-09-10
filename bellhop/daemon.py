"""Resident watchdog and sentinel: deterministic service installation for launchd and systemd.

`install` writes service files and (only with `load=True`) loads them. The service points at a
vendored copy of Bellhop inside the desk (`<desk>/.bellhop/`) so that plugin upgrades, which move
the plugin cache directory, cannot break a running service.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from .config import FRAMEWORK_ROOT, Desk
from .util import BellhopError, read_json, run, slugify, ts, write, write_json


def _run_quiet(cmd):
    """Run a service-manager command; a missing binary is reported like a failed command."""
    try:
        return run(cmd, check=False)
    except (FileNotFoundError, OSError) as exc:
        class _Missing:
            returncode = 127
            stdout = ""
            stderr = f"{cmd[0]}: not found ({exc})"
        return _Missing()

VENDOR_DIR = ".bellhop"
HEARTBEAT = "bells/.heartbeat"


# -- heartbeat -----------------------------------------------------------------
def touch_heartbeat(desk: Desk, who: str = "resident") -> None:
    p = desk.path(HEARTBEAT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{who} {ts()}\n", encoding="utf-8")


def heartbeat_age(desk: Desk) -> Optional[float]:
    p = desk.path(HEARTBEAT)
    if not p.exists():
        return None
    return max(0.0, time.time() - p.stat().st_mtime)


def heartbeat_owner(desk: Desk) -> Optional[str]:
    p = desk.path(HEARTBEAT)
    if not p.exists():
        return None
    return (p.read_text(encoding="utf-8").split() or [None])[0]


def resident_alive(desk: Desk, fresh_s: int = 120) -> bool:
    age = heartbeat_age(desk)
    return age is not None and age < fresh_s and heartbeat_owner(desk) == "resident"


# -- vendoring -----------------------------------------------------------------
def vendor(desk: Desk) -> Path:
    dst = desk.path(VENDOR_DIR)
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "bellhop").mkdir(parents=True)
    for src_rel in ("bellhop", "template/templates"):
        src = FRAMEWORK_ROOT / src_rel
        shutil.copytree(src, dst / src_rel, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), dirs_exist_ok=True)
    shutil.copy2(FRAMEWORK_ROOT / "bellhop_main.py", dst / "bellhop_main.py")
    from . import __version__
    write(dst / "VERSION", f"{__version__}\n")
    write(dst / "README.md", "Vendored copy of Bellhop used by the resident watchdog. Do not edit; run `bellhop daemon install` to refresh.\n")
    return dst


def vendored_version(desk: Desk) -> Optional[str]:
    p = desk.path(VENDOR_DIR, "VERSION")
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


# -- platform --------------------------------------------------------------------
def platform_name() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return "unsupported"


def service_dir(platform: str) -> Path:
    override = os.environ.get("BELLHOP_SERVICE_DIR")
    if override:
        return Path(override)
    if platform == "launchd":
        return Path.home() / "Library" / "LaunchAgents"
    return Path.home() / ".config" / "systemd" / "user"


def label(desk: Desk) -> str:
    return f"com.frostnova.bellhop.{slugify(desk.root.name, 32)}"


def _tool_path(name: str) -> Optional[str]:
    return shutil.which(name)


def _search_path(python: str, claude_bin: str) -> str:
    dirs = []
    for p in (python, _tool_path(claude_bin) or ""):
        if p:
            d = str(Path(p).resolve().parent)
            if d not in dirs:
                dirs.append(d)
    for d in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"):
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def autostart(desk: Desk) -> bool:
    """Do the resident services come up on their own at boot or login?

    Default no. A desk whose services come back by themselves after a reboot answers
    bells against whatever state the machine came up in, and the operator finds out from
    the receipts. The default is that a person brings them back: `daemon install --load`,
    or the load commands `daemon install` prints.
    """
    return bool(desk.cfg.get("autostart", False))


def service_files(desk: Desk, platform: str, python: Optional[str] = None) -> Dict[Path, str]:
    python = python or sys.executable
    main = str(desk.path(VENDOR_DIR, "bellhop_main.py"))
    logs = desk.bells_logs
    path_env = _search_path(python, desk.cfg["claude_bin"])
    lab = label(desk)
    auto = autostart(desk)
    files: Dict[Path, str] = {}
    sdir = service_dir(platform)
    if platform == "launchd":
        watch = {
            "Label": lab,
            "ProgramArguments": [python, main, "--desk", str(desk.root), "watch", "--loop"],
            "WorkingDirectory": str(desk.root),
            "EnvironmentVariables": {"PATH": path_env, "HOME": str(Path.home()), "LANG": "en_US.UTF-8"},
            # RunAtLoad starts it when the agent is loaded (which login does by itself);
            # KeepAlive brings it back after any exit, which also means "at boot".
            "RunAtLoad": auto,
            "KeepAlive": auto,
            "StandardOutPath": str(logs / "daemon.out.log"),
            "StandardErrorPath": str(logs / "daemon.err.log"),
        }
        sentinel = {
            "Label": lab + ".sentinel",
            "ProgramArguments": [python, main, "--desk", str(desk.root), "sentinel"],
            "WorkingDirectory": str(desk.root),
            "EnvironmentVariables": {"PATH": path_env, "HOME": str(Path.home()), "LANG": "en_US.UTF-8"},
            "RunAtLoad": auto,
            "StartInterval": int(desk.cfg["sentinel_interval_minutes"]) * 60,
            "StandardOutPath": str(logs / "sentinel.out.log"),
            "StandardErrorPath": str(logs / "sentinel.err.log"),
        }
        if not auto:
            # A StartInterval job that login loads would tick anyway, so with autostart
            # off both jobs are written disabled and the load commands enable them for
            # this session. Loaded-but-idle and not-loaded look the same to the operator
            # otherwise, and only one of them survives a reboot.
            watch["Disabled"] = True
            sentinel["Disabled"] = True
        files[sdir / f"{lab}.plist"] = plistlib.dumps(watch, sort_keys=False).decode("utf-8")
        files[sdir / f"{lab}.sentinel.plist"] = plistlib.dumps(sentinel, sort_keys=False).decode("utf-8")
    elif platform == "systemd":
        unit = lab
        # No [Install] section means `systemctl --user enable` has nothing to hook into,
        # so the unit can only be started by hand. With autostart on it is wanted by the
        # user's default target and comes up at login.
        watch_install = "\n[Install]\nWantedBy=default.target\n" if auto else ""
        timer_install = "\n[Install]\nWantedBy=timers.target\n" if auto else ""
        boot_sec = "OnBootSec=2min\n" if auto else ""
        files[sdir / f"{unit}.service"] = (
            "[Unit]\nDescription=Bellhop watchdog for %s\nAfter=default.target\n\n[Service]\nType=simple\n"
            "WorkingDirectory=%s\nEnvironment=PATH=%s\nEnvironment=LANG=C.UTF-8\nExecStart=%s %s --desk %s watch --loop\n"
            "Restart=always\nRestartSec=5\nStandardOutput=append:%s\nStandardError=append:%s\n%s"
            % (desk.root, desk.root, path_env, python, main, desk.root, logs / "daemon.out.log", logs / "daemon.err.log", watch_install)
        )
        files[sdir / f"{unit}-sentinel.service"] = (
            "[Unit]\nDescription=Bellhop sentinel for %s\n\n[Service]\nType=oneshot\nWorkingDirectory=%s\nEnvironment=PATH=%s\n"
            "ExecStart=%s %s --desk %s sentinel\nStandardOutput=append:%s\nStandardError=append:%s\n"
            % (desk.root, desk.root, path_env, python, main, desk.root, logs / "sentinel.out.log", logs / "sentinel.err.log")
        )
        files[sdir / f"{unit}-sentinel.timer"] = (
            "[Unit]\nDescription=Run the Bellhop sentinel every %d minutes\n\n[Timer]\n%sOnUnitActiveSec=%dmin\nUnit=%s-sentinel.service\n%s"
            % (int(desk.cfg["sentinel_interval_minutes"]), boot_sec, int(desk.cfg["sentinel_interval_minutes"]), unit, timer_install)
        )
    else:
        raise BellhopError(f"no resident-service support for platform {sys.platform}; run `bellhop watch --loop` under your own supervisor")
    return files


def load_commands(desk: Desk, platform: str) -> list:
    """What starts the services *now*. With autostart off, this is the only thing that does."""
    lab = label(desk)
    sdir = service_dir(platform)
    uid = os.getuid()
    auto = autostart(desk)
    if platform == "launchd":
        cmds = []
        for target in (lab, f"{lab}.sentinel"):
            plist = sdir / (f"{lab}.plist" if target == lab else f"{lab}.sentinel.plist")
            if not auto:
                cmds.append(["launchctl", "enable", f"gui/{uid}/{target}"])
            cmds.append(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
            if not auto:
                cmds.append(["launchctl", "kickstart", f"gui/{uid}/{target}"])
        return cmds
    if auto:
        return [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", f"{lab}.service", f"{lab}-sentinel.timer"],
        ]
    return [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "start", f"{lab}.service", f"{lab}-sentinel.timer"],
    ]


def unload_commands(desk: Desk, platform: str) -> list:
    lab = label(desk)
    uid = os.getuid()
    auto = autostart(desk)
    if platform == "launchd":
        cmds = [["launchctl", "bootout", f"gui/{uid}/{lab}"], ["launchctl", "bootout", f"gui/{uid}/{lab}.sentinel"]]
        if not auto:
            # Put the job back where the plist says it should be: `launchctl enable` is
            # remembered across reboots, so leaving it enabled would quietly undo the
            # autostart setting for the next install.
            cmds.append(["launchctl", "disable", f"gui/{uid}/{lab}"])
            cmds.append(["launchctl", "disable", f"gui/{uid}/{lab}.sentinel"])
        return cmds
    if auto:
        return [["systemctl", "--user", "disable", "--now", f"{lab}.service", f"{lab}-sentinel.timer"]]
    return [["systemctl", "--user", "stop", f"{lab}.service", f"{lab}-sentinel.timer"]]


def marker_path(desk: Desk) -> Path:
    return desk.state / ".daemon.json"


def install(desk: Desk, load: bool = False, python: Optional[str] = None) -> dict:
    platform = platform_name()
    vendored = vendor(desk)
    files = service_files(desk, platform, python)
    for path, content in files.items():
        write(path, content)
    marker = {
        "platform": platform,
        "label": label(desk),
        "service_files": [str(p) for p in files],
        "vendored": str(vendored),
        "vendored_version": vendored_version(desk),
        "python": python or sys.executable,
        "installed_at": ts(),
        "autostart": autostart(desk),
        "loaded": False,
    }
    report = {
        "platform": platform,
        "files": [str(p) for p in files],
        "load_commands": [" ".join(c) for c in load_commands(desk, platform)],
        "autostart": autostart(desk),
        "loaded": False,
        "errors": [],
    }
    if load:
        for cmd in load_commands(desk, platform):
            proc = _run_quiet(cmd)
            if proc.returncode != 0:
                report["errors"].append(f"{' '.join(cmd)} -> {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()}")
        report["loaded"] = not report["errors"]
        marker["loaded"] = report["loaded"]
    write_json(marker_path(desk), marker)
    return report


def uninstall(desk: Desk) -> dict:
    marker = read_json(marker_path(desk), None)
    if not marker:
        raise BellhopError("no resident service recorded for this desk (state/.daemon.json missing)")
    platform = marker.get("platform", platform_name())
    errors = []
    for cmd in unload_commands(desk, platform):
        proc = _run_quiet(cmd)
        if proc.returncode != 0:
            errors.append(f"{' '.join(cmd)} -> {proc.returncode}")
    removed = []
    for f in marker.get("service_files", []):
        p = Path(f)
        if p.exists():
            p.unlink()
            removed.append(f)
    marker_path(desk).unlink()
    return {"removed": removed, "errors": errors}


def status(desk: Desk) -> dict:
    marker = read_json(marker_path(desk), None)
    platform = (marker or {}).get("platform", platform_name())
    running = None
    if marker:
        lab = marker.get("label", label(desk))
        if platform == "launchd":
            proc = _run_quiet(["launchctl", "print", f"gui/{os.getuid()}/{lab}"])
            running = proc.returncode == 0
        elif platform == "systemd":
            proc = _run_quiet(["systemctl", "--user", "is-active", f"{lab}.service"])
            running = (proc.stdout or "").strip() == "active"
    hb = heartbeat_age(desk)
    from . import __version__
    return {
        "installed": bool(marker),
        "platform": platform,
        "label": (marker or {}).get("label"),
        "autostart": autostart(desk),
        "autostart_at_install": (marker or {}).get("autostart"),
        "loaded_at_install": (marker or {}).get("loaded"),
        "service_running": running,
        "heartbeat_age_s": None if hb is None else round(hb),
        "heartbeat_fresh": hb is not None and hb < 120,
        "vendored_version": vendored_version(desk),
        "framework_version": __version__,
        "vendored_stale": vendored_version(desk) not in (None, __version__),
        "service_files": (marker or {}).get("service_files", []),
    }


def status_text(desk: Desk) -> str:
    s = status(desk)
    lines = [f"resident service: {'installed' if s['installed'] else 'not installed'} ({s['platform']})"]
    if s["installed"]:
        lines.append(f"  label: {s['label']}")
        lines.append(f"  service running: {s['service_running'] if s['service_running'] is not None else 'unknown'}")
    lines.append(
        "  autostart: "
        + ("yes (comes up at boot/login)" if s["autostart"] else "no (after a reboot, start them by hand: bellhop daemon install --load)")
    )
    if s["installed"] and s["autostart_at_install"] is not None and s["autostart_at_install"] != s["autostart"]:
        lines.append(
            f"    the installed service files were written with autostart={'yes' if s['autostart_at_install'] else 'no'}; reinstall to apply the change"
        )
    lines.append(f"  heartbeat: {'none' if s['heartbeat_age_s'] is None else str(s['heartbeat_age_s']) + 's ago'}{' (fresh)' if s['heartbeat_fresh'] else ''}")
    lines.append(f"  vendored version: {s['vendored_version'] or '-'} (framework {s['framework_version']}){' STALE, reinstall' if s['vendored_stale'] else ''}")
    return "\n".join(lines) + "\n"
