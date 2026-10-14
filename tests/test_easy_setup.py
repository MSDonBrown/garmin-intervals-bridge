"""0.8.0: the home folder, the log file, autostart, update notice, login note, the setup wizard."""
import os
import plistlib
import stat
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

from garmin_intervals_bridge import __version__, alerts, autostart, cli, config, notices, wizard
from garmin_intervals_bridge.garmin import GarminLoginNeeded
from garmin_intervals_bridge.store import Store, single_instance

# ---- the home folder and the log file ----


def test_settings_come_from_the_home_folder_from_any_working_folder(_empty_home, tmp_path, monkeypatch):
    folder = _empty_home / "GarminBridge"
    folder.mkdir()
    (folder / ".env").write_text("INTERVALS_API_KEY=k\nBRIDGE_TIMEZONE=Europe/Dublin\nBRIDGE_LOG_FILE=bridge.log\n")
    monkeypatch.chdir(tmp_path)                                    # somewhere else, no .env here
    for name in ("INTERVALS_API_KEY", "BRIDGE_TIMEZONE", "BRIDGE_DATA_DIR", "GARMIN_TOKEN_DIR", "BRIDGE_LOG_FILE"):
        monkeypatch.delenv(name, raising=False)
    settings = config.Settings.from_env()
    assert settings.env_file == (folder / ".env").resolve()
    assert settings.data_dir == (folder / "data").resolve()        # relative to the settings file, not to cwd
    assert settings.token_dir == (folder / "data" / "tokens").resolve()
    assert settings.log_file == (folder / "bridge.log").resolve()
    assert settings.timezone.key == "Europe/Dublin"


def test_a_dot_env_in_the_working_folder_still_comes_first(_empty_home, tmp_path, monkeypatch):
    (_empty_home / "GarminBridge").mkdir()
    (_empty_home / "GarminBridge" / ".env").write_text("INTERVALS_API_KEY=home\n")
    (tmp_path / ".env").write_text("INTERVALS_API_KEY=here\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    assert config.env_file() == Path(".env")
    assert config.Settings.from_env().intervals_api_key == "here"


def test_without_any_settings_file_paths_stay_relative_to_the_working_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BRIDGE_DATA_DIR", raising=False)
    monkeypatch.delenv("GARMIN_TOKEN_DIR", raising=False)
    settings = config.Settings.from_env()                           # docker, systemd: as before
    assert settings.env_file is None and settings.data_dir == (tmp_path / "data").resolve()


def test_env_file_option_and_log_file_also_catch_a_broken_setting(tmp_path, monkeypatch):
    env = tmp_path / "bridge" / "my.env"
    env.parent.mkdir()
    env.write_text("BRIDGE_LOG_FILE=logs/bridge.log\nBRIDGE_GARMIN_REQUEST_DELAY=0.01\n")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--env-file", str(env), "status"]) == 1
    log = (env.parent / "logs" / "bridge.log").read_text()
    assert "BRIDGE_GARMIN_REQUEST_DELAY must be >= 0.25" in log      # the windowless bridge leaves a trace


def test_flags_default_on_and_say_what_is_wrong(monkeypatch):
    monkeypatch.delenv("BRIDGE_UPDATE_CHECK", raising=False)
    assert config._flag("BRIDGE_UPDATE_CHECK", True) is True
    monkeypatch.setenv("BRIDGE_UPDATE_CHECK", "off")
    assert config._flag("BRIDGE_UPDATE_CHECK", True) is False
    monkeypatch.setenv("BRIDGE_UPDATE_CHECK", "maybe")
    with pytest.raises(ValueError, match="true or false"):
        config._flag("BRIDGE_UPDATE_CHECK", True)


# ---- autostart ----


class Runner:
    def __init__(self, fail=()):
        self.calls, self.fail = [], fail

    def __call__(self, cmd):
        self.calls.append(cmd)
        if any(cmd[:len(f)] == f for f in self.fail):
            raise RuntimeError("not loaded")
        return SimpleNamespace(stdout="Status: Running\n")


def test_windows_task_runs_at_your_logon_without_limits_and_without_a_window(tmp_path, monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "PC")
    monkeypatch.setenv("USERNAME", "ann")
    env = tmp_path / "Garmin Bridge" / ".env"
    env.parent.mkdir()
    run = Runner(fail=(["schtasks", "/End"],))                       # nothing running yet
    steps = autostart.install(env, runner=run, kind="windows")
    xml = (env.parent / autostart.TASK_XML).read_text(encoding="utf-16")
    assert "<LogonTrigger><Enabled>true</Enabled><UserId>PC\\ann</UserId></LogonTrigger>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml               # not stopped after 72 h
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml                          # no admin rights
    assert f'--env-file "{env.resolve()}" run --apply</Arguments>' in xml          # quoted: a space
    assert [c[1] for c in run.calls] == ["/End", "/Create", "/Run"]
    assert run.calls[1][-3:] == ["/XML", str(env.parent.resolve() / autostart.TASK_XML), "/F"]
    assert steps["kind"] == "windows"
    run = Runner(fail=(["schtasks", "/End"],))
    autostart.remove(env, runner=run, kind="windows")
    assert [c[1] for c in run.calls] == ["/End", "/Delete"]
    assert not (env.parent / autostart.TASK_XML).exists() and env.parent.exists()


def test_windows_uses_pythonw_next_to_python(tmp_path, monkeypatch):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert autostart._python(windowless=True).endswith("pythonw.exe")
    assert autostart._python().endswith("python.exe")


def test_macos_launch_agent_restarts_after_a_crash(tmp_path, _empty_home):
    env = tmp_path / ".env"
    run = Runner(fail=(["launchctl", "bootout"],))                   # first install: nothing loaded
    autostart.install(env, runner=run, home=_empty_home, kind="macos")
    plist_path = _empty_home / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist"
    agent = plistlib.loads(plist_path.read_bytes())
    assert agent["ProgramArguments"][1:] == ["-m", "garmin_intervals_bridge", "--env-file", str(env.resolve()),
                                             "run", "--apply"]
    assert agent["RunAtLoad"] and agent["KeepAlive"] == {"SuccessfulExit": False}
    assert "StandardOutPath" not in agent                            # the bridge writes its own log file
    assert [c[1] for c in run.calls] == ["bootout", "bootstrap"]
    autostart.remove(env, runner=Runner(), home=_empty_home, kind="macos")
    assert not plist_path.exists()


def test_macos_bootstrap_waits_for_the_old_agent_to_go(tmp_path, _empty_home, monkeypatch):
    monkeypatch.setattr(autostart.time, "sleep", lambda s: None)
    tries = []

    def run(cmd):
        if cmd[1] == "bootstrap":
            tries.append(cmd)
            if len(tries) < 3:
                raise RuntimeError("Bootstrap failed: 5: Input/output error")
    autostart.install(tmp_path / ".env", runner=run, home=_empty_home, kind="macos")
    assert len(tries) == 3


def test_linux_user_service(tmp_path, _empty_home, monkeypatch):
    monkeypatch.setattr(autostart.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/systemctl")
    env = tmp_path / ".env"
    run = Runner()
    autostart.install(env, runner=run, home=_empty_home, kind="linux", mode="upload")
    unit = (_empty_home / ".config" / "systemd" / "user" / autostart.UNIT).read_text()
    assert f"--env-file {env.resolve()} run --apply --mode upload" in unit and "Restart=on-failure" in unit
    assert run.calls[-1] == ["systemctl", "--user", "restart", autostart.UNIT]
    assert autostart.status(env, runner=run, home=_empty_home, kind="linux")["installed"] is True


def test_linux_as_root_and_containers_get_pointed_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(ValueError, match="deploy/systemd"):
        autostart.install(tmp_path / ".env", runner=Runner(), kind="linux")
    with pytest.raises(ValueError, match="restart"):
        autostart.install(tmp_path / ".env", runner=Runner(), kind="container")
    assert autostart.status(tmp_path / ".env", kind="container")["installed"] is False


def test_autostart_command_needs_a_settings_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["autostart", "--dry-run"]) == 1                 # no settings file: run setup first


def test_autostart_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("INTERVALS_API_KEY=k\n")
    monkeypatch.setattr(autostart, "_run", lambda cmd: pytest.fail("dry run must not run anything"))
    monkeypatch.setattr(autostart, "platform", lambda: "macos")
    assert cli.main(["--env-file", str(env), "autostart", "--dry-run"]) == 0
    assert "bootstrap" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]                  # not even a data folder


# ---- update notice ----


class PyPI:
    def __init__(self, version=None, error=None):
        self.version, self.error = version, error

    def get(self, url, timeout):
        assert url == notices.PYPI_URL and timeout
        if self.error:
            raise self.error
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"info": {"version": self.version}})


def test_update_check_offers_only_a_newer_final_release():
    assert notices.newer_release("0.8.0", session=PyPI("0.8.1")) == "0.8.1"
    assert notices.newer_release("0.8.0", session=PyPI("0.10.0")) == "0.10.0"      # numbers, not text
    assert notices.newer_release("0.8.0", session=PyPI("0.8.0")) is None
    assert notices.newer_release("0.8.1", session=PyPI("0.8.0")) is None
    assert notices.newer_release("0.8.0", session=PyPI("0.9.0rc1")) is None
    assert notices.newer_release("0.8.0", session=PyPI(error=requests.ConnectionError())) is None


def test_update_finding_names_the_way_to_update_and_can_be_switched_off():
    on = SimpleNamespace(update_check=True)
    finding = notices.update_finding(on, in_container=False, session=PyPI("99.0.0"))
    assert finding["alert"] is False and "pip install --upgrade" in finding["detail"]
    assert __version__ in finding["detail"]
    assert "pull the image" in notices.update_finding(on, in_container=True, session=PyPI("99.0.0"))["detail"]
    off = SimpleNamespace(update_check=False)
    assert notices.update_finding(off, in_container=False, session=PyPI("99.0.0")) is None


# ---- the login note ----


class NoteIntervals:
    def __init__(self):
        self.notes = []

    def create_note(self, day, name, description):
        self.notes.append((day, name))


def test_login_note_once_per_episode_and_only_when_writing(tmp_path):
    st, iv = Store(tmp_path), NoteIntervals()
    on = SimpleNamespace(intervals_notes=True)
    assert notices.login_needed(on, st, iv, date(2026, 10, 12), apply=False) is False      # dry run: no write
    assert notices.login_needed(on, st, iv, date(2026, 10, 12), apply=True) is True
    assert notices.login_needed(on, st, iv, date(2026, 10, 13), apply=True) is False       # same episode
    notices.garmin_ok(st)                                                                  # signed in again
    assert notices.login_needed(on, st, iv, date(2026, 11, 2), apply=True) is True         # a new episode
    assert [d for d, _ in iv.notes] == [date(2026, 10, 12), date(2026, 11, 2)]
    assert notices.login_needed(SimpleNamespace(intervals_notes=False), Store(tmp_path / "b"), iv,
                                date(2026, 11, 3), apply=True) is False
    st.close()


def test_run_loop_puts_the_note_in_the_calendar_when_garmin_wants_a_login(tmp_path, monkeypatch):
    iv = NoteIntervals()
    monkeypatch.setattr(cli, "IntervalsClient", lambda *a, **k: iv)

    def enrich(*a, **k):
        raise GarminLoginNeeded("Garmin login needed")
    monkeypatch.setattr(cli, "sync_enrich", enrich)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    settings = SimpleNamespace(data_dir=tmp_path, token_dir=tmp_path / "tokens", intervals_api_key="k",
                               intervals_athlete_id="0", garmin_request_delay=0.5, timezone=ZoneInfo("Europe/Vienna"),
                               intervals_notes=True)
    st = Store(tmp_path)
    cli.run_loop(settings, st, apply=True, poll_seconds=60, sync_minutes=30, iterations=1)
    cli.run_loop(settings, st, apply=True, poll_seconds=60, sync_minutes=30, iterations=1)
    assert len(iv.notes) == 1 and iv.notes[0][1] == notices.LOGIN_NOTE_NAME
    st.close()


def test_create_note_posts_a_calendar_note():
    from garmin_intervals_bridge.intervals import IntervalsClient

    class Session:
        auth, headers = None, {}

        def request(self, method, url, timeout, **kwargs):
            self.sent = (method, url, kwargs["json"])
            return SimpleNamespace(raise_for_status=lambda: None, status_code=200, content=b"{}", json=lambda: {})
    session = Session()
    IntervalsClient("k", session=session).create_note(date(2026, 10, 12), "N", "D")
    method, url, body = session.sent
    assert method == "POST" and url.endswith("/athlete/0/events")
    assert body == {"category": "NOTE", "start_date_local": "2026-10-12T00:00:00", "name": "N", "description": "D"}


# ---- the setup wizard ----


class Script(wizard.Prompt):
    def __init__(self, answers, secrets=()):
        self.answers, self.secrets, self.said, self.asked = list(answers), list(secrets), [], []
        super().__init__(ask=self._next, secret=self._secret, say=self.said.append)

    def _next(self, question):
        self.asked.append(question)
        if not self.answers:
            pytest.fail(f"unexpected question: {question}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _secret(self, question):
        self.asked.append(question)
        return self.secrets.pop(0)


class WizardIntervals:
    def __init__(self, key, fields=()):
        self.key, self.fields, self.charts, self.created_fields = key, list(fields), [], []

    def athlete(self):
        if self.key != "good-key":
            response = requests.Response()
            response.status_code = 401
            raise requests.HTTPError("401", response=response)
        return {"id": "i1", "name": "Ann Example"}

    def custom_items(self):
        return self.fields

    def create_custom_item(self, item):
        self.created_fields.append(item)
        return {"id": len(self.created_fields)}

    def update_custom_item(self, item_id, item):
        pass

    def reorder_custom_items(self, items):
        pass

    def gear(self):
        return []


class WizardGarmin:
    logins: list = []

    def __init__(self, token_dir, delay):
        self.token_dir = token_dir

    def login(self, interactive=True):
        WizardGarmin.logins.append(interactive)
        if not interactive and not (self.token_dir / "ok").exists():
            raise GarminLoginNeeded("Garmin login needed")
        self.token_dir.mkdir(parents=True, exist_ok=True)
        (self.token_dir / "ok").write_text("")


def _wizard(script, monkeypatch, **kw):
    monkeypatch.setattr(wizard.autostart, "platform", lambda: "linux")
    monkeypatch.setattr("tzlocal.get_localzone_name", lambda: "Europe/Dublin")
    clients = {}

    def client(key):
        clients[key] = clients.get(key) or WizardIntervals(key)
        return clients[key]
    installed = []
    notify = kw.pop("notify", lambda channel, values: "sent")
    trial = kw.pop("trial", lambda *a: {"activities": {"seen": 2, "planned": 1},
                                       "wellness": {"days_checked": 1, "days_with_changes": 1}})
    WizardGarmin.logins = []
    code = wizard.run(script, intervals_client=client, garmin_source=WizardGarmin,
                      install=lambda env, **k: installed.append(env) or {"note": ""}, trial=trial, notify=notify)
    return code, installed, clients


def test_setup_from_nothing_to_running(_empty_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("INTERVALS_API_KEY", "BRIDGE_TIMEZONE", "BRIDGE_DATA_DIR", "GARMIN_TOKEN_DIR", "BRIDGE_LOG_FILE"):
        monkeypatch.delenv(name, raising=False)
    script = Script(answers=["", "",        # folder: ~/GarminBridge; 1 = Intervals
                             "",            # time zone Europe/Dublin right
                             "n",           # gear
                             "n",           # trend
                             "n",           # charts
                             "n",           # no swap
                             "n", "n",      # no e-mail, no push
                             "y"],          # start
                    secrets=["wrong-key-of-full-length", "good-key"])
    code, installed, clients = _wizard(script, monkeypatch)
    assert code == 0
    env = (_empty_home / "GarminBridge" / ".env").resolve()
    assert installed == [env]
    values = config.parse_env_file(env)
    assert values["INTERVALS_API_KEY"] == "good-key" and values["BRIDGE_TIMEZONE"] == "Europe/Dublin"
    assert values["BRIDGE_LOG_FILE"] == "bridge.log" and values["BRIDGE_IMREADY4_ENABLED"] == "false"
    if os.name == "posix":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    said = "\n".join(script.said)
    assert "Intervals refused this key" in said and "Connected to Intervals.icu as Ann Example" in said
    assert "good-key" not in said and "good-key" not in "".join(script.asked)          # never shown
    assert WizardGarmin.logins == [False, True]                                       # signed in once
    assert "would get what Garmin's own sync leaves out" in said and "runs in the background" in said


def test_setup_again_keeps_the_answers_and_foreign_lines(_empty_home, tmp_path, monkeypatch):
    folder = _empty_home / "GarminBridge"
    folder.mkdir()
    (folder / "data" / "tokens").mkdir(parents=True)
    (folder / "data" / "tokens" / "ok").write_text("")
    env = folder / ".env"
    env.write_text("# mine\nINTERVALS_API_KEY=good-key\nBRIDGE_TIMEZONE=Europe/Vienna\nBRIDGE_STALE_HOURS=12\n")
    monkeypatch.chdir(tmp_path)
    script = Script(answers=["", "",        # 1 = Intervals; keep the key
                             "",            # Europe/Vienna right
                             "n", "n", "n", "n",
                             "n", "n",      # no e-mail, no push
                             "n"])          # do not start yet
    code, installed, clients = _wizard(script, monkeypatch)
    assert code == 0 and installed == []
    text = env.read_text()
    assert text.startswith("# mine\nINTERVALS_API_KEY=good-key\nBRIDGE_TIMEZONE=Europe/Vienna\nBRIDGE_STALE_HOURS=12\n")
    assert "BRIDGE_LOG_FILE=bridge.log" in text
    assert WizardGarmin.logins == [False]                                            # saved session used
    assert "Nothing written to Intervals" in "\n".join(script.said)


def test_setup_ignores_another_projects_dot_env(_empty_home, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DATABASE_URL=postgres://x\n")
    monkeypatch.chdir(tmp_path)
    script = Script(answers=[str(tmp_path / "bridge"), "", "", "n", "n", "n", "n", "n", "n", "n"], secrets=["good-key"])
    code, _, _ = _wizard(script, monkeypatch)
    assert code == 0 and (tmp_path / ".env").read_text() == "DATABASE_URL=postgres://x\n"
    assert config.parse_env_file(tmp_path / "bridge" / ".env")["INTERVALS_API_KEY"] == "good-key"


def test_setup_while_the_bridge_runs_skips_the_trial_and_restarts_it(_empty_home, tmp_path, monkeypatch):
    folder = _empty_home / "GarminBridge"
    (folder / "data" / "tokens").mkdir(parents=True)
    (folder / "data" / "tokens" / "ok").write_text("")
    (folder / ".env").write_text("INTERVALS_API_KEY=good-key\nBRIDGE_TIMEZONE=Europe/Vienna\n")
    monkeypatch.chdir(tmp_path)
    script = Script(answers=["", "", "", "n", "n", "n", "n", "n", "n", "y"])
    with single_instance((folder / "data").resolve(), ("activities",)):
        code, installed, _ = _wizard(script, monkeypatch, trial=lambda *a: pytest.fail("no trial next to it"))
    assert code == 0 and len(installed) == 1 and "(restarted)" in "\n".join(script.said)


def test_setup_creates_charts_and_trend_fields_only_after_the_last_yes(_empty_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = Script(answers=["", "", "", "n", "y", "y", "y", "n", "n", "y"], secrets=["good-key"])   # trend, charts, swap
    code, _, clients = _wizard(script, monkeypatch)
    iv = clients["good-key"]
    assert code == 0 and iv.created_fields                       # trend fields and charts
    values = config.parse_env_file(_empty_home / "GarminBridge" / ".env")
    assert values["BRIDGE_HRVTREND_ENABLED"] == "true" and values["BRIDGE_SWAP"] == "true"


def test_setup_stopped_with_ctrl_c_keeps_what_was_answered(_empty_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = Script(answers=["", "", "", KeyboardInterrupt()], secrets=["good-key"])
    code, installed, _ = _wizard(script, monkeypatch)
    assert code == 1 and installed == []
    assert "Setup stopped" in script.said[-1]
    assert config.parse_env_file(_empty_home / "GarminBridge" / ".env")["INTERVALS_API_KEY"] == "good-key"


def test_setup_refuses_inside_a_container(monkeypatch):
    monkeypatch.setattr(wizard.autostart, "platform", lambda: "container")
    script = Script(answers=[])
    assert wizard.run(script) == 1 and "container" in script.said[0]


def test_write_env_replaces_only_its_own_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep\nexport INTERVALS_API_KEY=old\nOTHER=1\n\n")
    wizard.write_env(env, {"INTERVALS_API_KEY": "new", "BRIDGE_TIMEZONE": "UTC"})
    assert env.read_text() == "# keep\nINTERVALS_API_KEY=new\nOTHER=1\n\nBRIDGE_TIMEZONE=UTC\n"
    assert not (tmp_path / ".env.tmp").exists()


def test_setup_turns_gear_on_for_new_activities_and_leaves_an_earlier_choice_alone(_empty_home, tmp_path, monkeypatch):
    from garmin_intervals_bridge.gear import load_config
    monkeypatch.chdir(tmp_path)
    script = Script(answers=["", "", "", "y", "n", "n", "n", "n", "n", "n", "y"], secrets=["good-key"])  # gear y, retired n
    code, _, _ = _wizard(script, monkeypatch)
    data = (_empty_home / "GarminBridge" / "data").resolve()
    st = Store(data)
    assert code == 0 and load_config(st)["create"] is True and load_config(st)["include_retired"] is False
    st.close()
    script = Script(answers=["", "", "", "n", "n", "n", "n", "n", "y"])   # again: no gear question any more
    code, _, _ = _wizard(script, monkeypatch)
    assert code == 0 and "Gear: set up earlier" in "\n".join(script.said)


def test_the_templates_are_well_formed(tmp_path):
    from xml.dom import minidom
    minidom.parseString(autostart.windows_task(tmp_path / ".env", user="PC\\ann").replace(
        'encoding="UTF-16"', 'encoding="UTF-8"'))
    root = Path(__file__).resolve().parents[1]
    minidom.parse(str(root / "deploy" / "unraid" / "garmin-intervals-bridge.xml"))
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load((root / "compose.yaml").read_text())["services"]["bridge"]["command"] == ["run"]
    guide = (root / "docs" / "SYNOLOGY.md").read_text()
    project = yaml.safe_load(guide.split("```yaml\n", 1)[1].split("```", 1)[0])
    bridge = project["services"]["bridge"]
    assert bridge["command"] == ["run"] and bridge["volumes"] == ["./data:/data"]
    assert set(bridge["environment"]) == {"INTERVALS_API_KEY", "BRIDGE_TIMEZONE", "BRIDGE_UI_PORT",
                                          "BRIDGE_UI_PASSWORD"}
    assert bridge["environment"]["BRIDGE_UI_PASSWORD"] == "change-me-please"           # refused as it is


def test_setup_names_commands_the_way_this_person_starts_the_bridge(_empty_home, monkeypatch):
    monkeypatch.setattr(wizard.autostart, "platform", lambda: "windows")
    assert wizard.command_name() == "py -m garmin_intervals_bridge"
    monkeypatch.setattr(wizard.autostart, "platform", lambda: "macos")
    venv = _empty_home / "GarminBridge" / "venv" / "bin" / "garmin-intervals-bridge"
    monkeypatch.setattr(sys, "argv", [str(venv), "setup"])
    monkeypatch.setattr(wizard.shutil, "which", lambda name: None)
    assert wizard.command_name() == str(Path("~/GarminBridge/venv/bin/garmin-intervals-bridge"))
    monkeypatch.setattr(wizard.shutil, "which", lambda name: str(venv))
    assert wizard.command_name() == "garmin-intervals-bridge"


def test_setup_sets_up_e_mail_and_push_alerts_after_a_test_message(_empty_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = []

    def notify(channel, values):
        sent.append((channel, dict(values)))
        return "the mailbox refused the user name or password" if len(sent) == 1 else "sent"
    script = Script(answers=["", "", "", "n", "n", "n", "n",
                             "y", "ann@gmail.com",            # e-mail: Gmail, the server is known
                             "y", "",                         # first password refused: try again, same address
                             "y", "",                         # push: subscribed, Enter
                             "n"],                            # do not start yet
                    secrets=["good-key", "wrong", "abcd efgh ijkl mnop"])
    code, _, _ = _wizard(script, monkeypatch, notify=notify)
    assert code == 0
    said = "\n".join(script.said)
    # whole lines, not substrings: the server named, Gmail's hint, the refusal
    assert "The bridge sends from your own mailbox to yourself, through smtp.gmail.com." in script.said
    assert alerts.HINTS[alerts.provider("ann@gmail.com")[0]] in script.said
    assert "Sending failed: the mailbox refused the user name or password." in script.said
    values = config.parse_env_file(_empty_home / "GarminBridge" / ".env")
    assert values["BRIDGE_ALERT_EMAIL"] == "ann@gmail.com" and values["BRIDGE_SMTP_PASSWORD"] == "abcd efgh ijkl mnop"
    assert "BRIDGE_SMTP_HOST" not in values                               # Gmail's server is built in
    assert values["BRIDGE_NTFY_URL"].startswith("https://ntfy.sh/garmin-bridge-")
    topic = values["BRIDGE_NTFY_URL"].rsplit("/", 1)[1]
    assert topic in said and [c for c, _ in sent] == ["e-mail", "e-mail", "push"]
    assert "abcd efgh" not in said                                        # the password is never shown

    script = Script(answers=["", "", "", "n", "n", "n", "n", "n", "y", "", "n"])   # again: e-mail off, push kept
    code, _, _ = _wizard(script, monkeypatch, notify=lambda c, v: "sent")
    values = config.parse_env_file(_empty_home / "GarminBridge" / ".env")
    assert values["BRIDGE_ALERT_EMAIL"] == "" and values["BRIDGE_SMTP_PASSWORD"] == ""
    assert values["BRIDGE_NTFY_URL"].endswith(topic)


def test_setup_asks_for_the_server_of_an_unknown_provider(_empty_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = Script(answers=["", "", "", "n", "n", "n", "n", "y", "me@example.org", "mail.example.org", "465", "",
                             "n", "n"], secrets=["good-key", "pw"])
    code, _, _ = _wizard(script, monkeypatch)
    values = config.parse_env_file(_empty_home / "GarminBridge" / ".env")
    assert code == 0 and values["BRIDGE_SMTP_HOST"] == "mail.example.org" and values["BRIDGE_SMTP_PORT"] == "465"
    assert values["BRIDGE_SMTP_USER"] == "me@example.org"


def test_setup_for_a_backup_without_intervals(_empty_home, tmp_path, monkeypatch):
    from garmin_intervals_bridge import backup
    monkeypatch.chdir(tmp_path)
    for name in ("INTERVALS_API_KEY", "BRIDGE_MODE", "BRIDGE_TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(wizard.autostart, "platform", lambda: "linux")
    monkeypatch.setattr("tzlocal.get_localzone_name", lambda: "Europe/Dublin")
    WizardGarmin.logins = []
    installed, backed_up = [], []
    script = Script(answers=["",             # folder
                             "2",            # only a backup
                             "",             # time zone right
                             "n", "n",       # no e-mail, no push
                             "y"])           # keep backing up in the background
    code = wizard.run(script, intervals_client=lambda key: pytest.fail("a backup needs no Intervals"),
                      garmin_source=WizardGarmin, notify=lambda c, v: "sent",
                      install=lambda env, **k: installed.append(k.get("mode")) or {"note": ""},
                      backup=lambda s, g, st: backed_up.append(1) or backup.new_metrics() | {"activities": 3})
    assert code == 0 and installed == ["backup"] and backed_up == [1]
    values = config.parse_env_file(_empty_home / "GarminBridge" / ".env")
    assert values["BRIDGE_MODE"] == "backup" and "INTERVALS_API_KEY" not in values
    said = "\n".join(script.said)
    assert "3 activities" in said and "backup --from" in said and "Intervals calendar" not in said


def test_what_a_paste_brings_along_is_no_part_of_the_key(monkeypatch):
    # Okocha in the forum: in the classic Windows console Ctrl+V reaches a hidden prompt as ^V
    said = []
    script = Script(answers=[], secrets=["\x16", "\x16\x16", '"good-key"\r\n'])
    script.say = said.append
    monkeypatch.setattr(wizard.os, "name", "nt")
    key, _ = wizard._intervals_key(script, "", lambda key: WizardIntervals(key))
    assert key == "good-key"
    assert said.count("Nothing arrived – paste with a right-click, then Enter.") == 2
    assert any("Paste it with a right-click" in line for line in said)
