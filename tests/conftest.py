import logging
import os
from logging.handlers import RotatingFileHandler

import pytest


@pytest.fixture(autouse=True)
def _empty_home(tmp_path_factory, monkeypatch):
    """No test may find a real ~/GarminBridge/.env of the machine it runs on."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("BRIDGE_ENV_FILE", raising=False)
    # no test talks to pypi.org or puts notes in a calendar unless it says so itself
    monkeypatch.setenv("BRIDGE_UPDATE_CHECK", "false")
    monkeypatch.setenv("BRIDGE_INTERVALS_NOTES", "false")
    return home


@pytest.fixture(autouse=True)
def _isolated_process():
    """`main` and `setup` set variables and add a log file handler, as they should in a real process."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, RotatingFileHandler)]:
        root.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def _normal_window(monkeypatch):
    """A CI runner may run elevated: the refusal of a Windows administrator window stays out of the
    other tests (test_admin_window.py checks it and the real token check)."""
    from garmin_intervals_bridge import autostart
    monkeypatch.setattr(autostart, "elevated_window", lambda: False)


LOCAL = ("127.0.0.1", "::1", "localhost")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test talks to the internet: a forgotten fake would otherwise reach Intervals or Garmin."""
    import socket
    real_connect, real_lookup = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in LOCAL:
            raise OSError(f"tests must not connect to {host}")
        return real_connect(self, address)

    def lookup(host, *args, **kwargs):
        if host not in LOCAL and host is not None:
            raise OSError(f"tests must not look up {host}")
        return real_lookup(host, *args, **kwargs)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
