"""Start the bridge in the background whenever you log in – without touching a scheduler by hand.

  Windows  a Task Scheduler task for your own account at logon, running `pythonw.exe -m
           garmin_intervals_bridge run --apply` (pythonw: no console window; Python itself is
           signed, so Smart App Control lets it run). Defined as XML because a plain
           `schtasks /SC ONLOGON` task needs admin rights, stops after 72 hours and on battery.
  macOS    a LaunchAgent in ~/Library/LaunchAgents, started again after a crash
  Linux    a systemd user service, started again after a crash

Every variant runs this very Python installation with the settings file `setup` wrote, so
it needs neither PATH nor a working folder; the bridge logs to the file BRIDGE_LOG_FILE
names. Only files this module wrote itself are ever removed. Containers bring their own
restart policy.
"""
from __future__ import annotations

import getpass
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

NAME = "Garmin Intervals Bridge"
LABEL = "at.futureweb.garmin-intervals-bridge"
UNIT = "garmin-intervals-bridge.service"
TASK_XML = "autostart-task.xml"


def platform() -> str:
    if os.getenv("BRIDGE_IN_CONTAINER") or Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return "container"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _python(windowless: bool = False) -> str:
    exe = Path(sys.executable)
    if windowless and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        return str(exe.with_name("pythonw.exe"))
    return str(exe)


def arguments(env_file: Path, mode: str = "enrich") -> list[str]:
    args = ["-m", "garmin_intervals_bridge", "--env-file", str(env_file), "run", "--apply"]
    return args + ["--mode", mode] if mode != "enrich" else args


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg or not arg else arg


def _windows_user() -> str:
    domain, user = os.getenv("USERDOMAIN", ""), os.getenv("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def windows_task(env_file: Path, mode: str = "enrich", user: str | None = None) -> str:
    """The task definition: your logon only, no time limit, on battery too, one instance."""
    user = escape(user or _windows_user())
    args = escape(" ".join(_quote(a) for a in arguments(env_file, mode)))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Garmin to Intervals.icu bridge, started at logon by
    'garmin-intervals-bridge autostart'. Remove with 'autostart --remove'.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RestartOnFailure><Interval>PT5M</Interval><Count>999</Count></RestartOnFailure>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_python(windowless=True))}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{escape(str(env_file.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def plan(env_file: Path, *, mode: str = "enrich", home: Path | None = None, kind: str | None = None) -> dict:
    """What `install` does on this computer: the files it writes and the commands it runs."""
    kind = kind or platform()
    home = home or Path.home()
    env_file = env_file.resolve()
    folder = env_file.parent
    if kind == "windows":
        xml = folder / TASK_XML
        return {"kind": kind, "files": {str(xml): windows_task(env_file, mode)}, "encoding": "utf-16",
                "install": [["schtasks", "/End", "/TN", NAME],          # an older one still running
                            ["schtasks", "/Create", "/TN", NAME, "/XML", str(xml), "/F"],
                            ["schtasks", "/Run", "/TN", NAME]],
                "remove": [["schtasks", "/End", "/TN", NAME], ["schtasks", "/Delete", "/TN", NAME, "/F"]],
                "remove_files": [str(xml)],
                "status": ["schtasks", "/Query", "/TN", NAME, "/FO", "LIST"],
                "log": "Task Scheduler (taskschd.msc) lists it as '" + NAME + "'"}
    if kind == "macos":
        plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        # no StandardOutPath: the bridge writes its own log file (BRIDGE_LOG_FILE), twice would double it
        content = plistlib.dumps({"Label": LABEL, "ProgramArguments": [_python()] + arguments(env_file, mode),
                                  "WorkingDirectory": str(folder), "RunAtLoad": True,
                                  "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 300,
                                  "ProcessType": "Background"}).decode()
        domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/0"
        return {"kind": kind, "files": {str(plist): content},
                "install": [["launchctl", "bootout", f"{domain}/{LABEL}"],
                            ["launchctl", "bootstrap", domain, str(plist)]],
                "remove": [["launchctl", "bootout", f"{domain}/{LABEL}"]],
                "remove_files": [str(plist)],
                "status": ["launchctl", "print", f"{domain}/{LABEL}"]}
    if kind == "linux":
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return _not_here(kind, "as root, use the system units in deploy/systemd instead (a dedicated "
                                   "user, hardening); autostart is for your own login")
        if shutil.which("systemctl") is None:
            return _not_here(kind, "no systemd here: start `garmin-intervals-bridge run --apply` the way your "
                                   "system starts programs at login")
        unit = home / ".config" / "systemd" / "user" / UNIT
        exec_start = " ".join(_quote(a) for a in [_python()] + arguments(env_file, mode)).replace("%", "%%")
        content = (f"[Unit]\nDescription={NAME}\nAfter=network-online.target\n\n"
                   f"[Service]\nWorkingDirectory={str(folder).replace('%', '%%')}\nExecStart={exec_start}\n"
                   "Restart=on-failure\nRestartSec=300\n\n[Install]\nWantedBy=default.target\n")
        return {"kind": kind, "files": {str(unit): content},
                "install": [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", UNIT],
                            ["systemctl", "--user", "restart", UNIT]],
                "remove": [["systemctl", "--user", "disable", "--now", UNIT]],
                "remove_files": [str(unit)],
                "after_remove": [["systemctl", "--user", "daemon-reload"]],
                "status": ["systemctl", "--user", "is-active", UNIT],
                "note": "it runs while you are logged in; to keep it running after logout: "
                        f"loginctl enable-linger {os.getenv('USER', '')}".rstrip()}
    return _not_here(kind, "a container starts the bridge itself: its command `run --apply` and restart "
                           "policy 'unless-stopped' (Synology: auto-restart on)")


def _not_here(kind: str, why: str) -> dict:
    return {"kind": kind, "files": {}, "install": [], "remove": [], "unsupported": why}


Runner = Callable[[list[str]], Any]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # Windows tools answer in the console's code page (cp850 on a German system: "ä" is 0x84),
    # not in UTF-8; and an odd byte must never stop the web page
    return subprocess.run(cmd, capture_output=True, text=True, check=True,
                          encoding="oem" if os.name == "nt" else None, errors="replace")


def _may_fail(cmd: list[str]) -> bool:
    """Stopping what is not running yet."""
    return cmd[:2] == ["launchctl", "bootout"] or cmd[:2] == ["schtasks", "/End"]


def _quietly(run: Runner, cmd: list[str]) -> None:
    try:
        run(cmd)
    except Exception:
        pass                                      # already stopped, not loaded, not there: fine


def install(env_file: Path, *, mode: str = "enrich", runner: Runner | None = None,
            home: Path | None = None, kind: str | None = None) -> dict:
    """Write the files, register and start; doing it again replaces the earlier one."""
    steps = plan(env_file, mode=mode, home=home, kind=kind)
    if steps.get("unsupported"):
        raise ValueError(f"No autostart on {steps['kind']}: {steps['unsupported']}")
    run = runner or _run
    for path, content in steps["files"].items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding=steps.get("encoding", "utf-8"))
    for cmd in steps["install"]:
        if _may_fail(cmd):
            _quietly(run, cmd)
        elif cmd[:2] == ["launchctl", "bootstrap"]:
            _bootstrap(run, cmd)
        else:
            run(cmd)
    return steps


def _bootstrap(run: Runner, cmd: list[str], attempts: int = 5, pause: float = 1.0) -> None:
    """launchd may still be tearing the old agent down ('Bootstrap failed: 5'): try again shortly."""
    for attempt in range(attempts):
        try:
            run(cmd)
            return
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(pause)


def remove(env_file: Path, *, runner: Runner | None = None, home: Path | None = None,
           kind: str | None = None) -> dict:
    """Stop and unregister; deletes only the task/agent/unit file autostart wrote."""
    steps = plan(env_file, home=home, kind=kind)
    if steps.get("unsupported"):
        raise ValueError(f"No autostart on {steps['kind']}: {steps['unsupported']}")
    run = runner or _run
    for cmd in steps["remove"]:
        _quietly(run, cmd)
    for path in steps.get("remove_files", []):
        Path(path).unlink(missing_ok=True)
    for cmd in steps.get("after_remove", []):
        _quietly(run, cmd)
    return steps


def status(env_file: Path, *, runner: Runner | None = None, home: Path | None = None,
           kind: str | None = None) -> dict:
    """Whether the scheduler knows the bridge, with what it says about it."""
    steps = plan(env_file, home=home, kind=kind)
    if steps.get("unsupported"):
        return {"kind": steps["kind"], "installed": False, "detail": steps["unsupported"]}
    try:
        out = (runner or _run)(steps["status"])
        detail = (getattr(out, "stdout", "") or "").strip()
        return {"kind": steps["kind"], "installed": True, "detail": "\n".join(detail.splitlines()[:12])}
    except Exception:
        return {"kind": steps["kind"], "installed": False, "detail": "not set up"}
