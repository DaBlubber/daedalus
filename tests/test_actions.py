# -*- coding: utf-8 -*-
"""Actions: what is refused before any packet leaves the house."""
import pytest

from daedalus import actions
from daedalus.actions import Refused, check_target


@pytest.fixture(autouse=True)
def networks():
    """The example site: own network 172.16.0.0/16, one ping-only network."""
    old = actions.OWN_NETWORKS, actions.PING_ONLY_NETWORKS
    actions.configure(["172.16.0.0/16"], ["192.168.100.0/24"])
    yield
    actions.OWN_NETWORKS, actions.PING_ONLY_NETWORKS = old


@pytest.mark.parametrize("target", ["host-59a8", "172.16.1.6; rm -rf /", "8.8.8.8",
                                    "172.16.10.255", "", "::1"])
def test_invalid_targets(target):
    with pytest.raises(Refused):
        check_target(target, "traceroute")


def test_ping_only_network_allows_only_ping():
    assert str(check_target("192.168.100.20", "ping")) == "192.168.100.20"
    with pytest.raises(Refused, match="only ping"):
        check_target("192.168.100.20", "port")


def test_command_gets_the_address_as_separate_argument(monkeypatch):
    seen = {}

    def run(command, **_k):
        seen["command"] = command

        class R:
            stdout, stderr, returncode = "4 packets transmitted, 4 received, 0% packet loss\n" \
                                         "rtt min/avg/max/mdev = 0.3/0.4/0.5/0.1 ms", "", 0
        return R()

    monkeypatch.setattr(actions.subprocess, "run", run)
    r = actions.ping("172.16.1.6")
    assert seen["command"][-1] == "172.16.1.6"
    assert r["ok"] and "0% packet loss" in r["summary"]


def test_wol_checks_the_mac():
    with pytest.raises(Refused):
        actions.wake_on_lan("not-a-mac")


def test_web_refuses_invalid_target_with_422():
    from fastapi.testclient import TestClient
    from daedalus.web import build_app
    client = TestClient(build_app(lambda: None, root="bb8"))
    r = client.post("/api/action/traceroute", json={"target": "8.8.8.8"})
    assert r.status_code == 422 and "own networks" in r.json()["detail"]
    assert client.post("/api/action/ping", json={"target": "1", "x": 1}).status_code == 422


def test_wol_needs_address_and_sends_unicast_first(monkeypatch):
    with pytest.raises(Refused, match="address"):
        actions.wake_on_lan("68:74:37:d4:c0:0b")
    sent = []

    class Sock:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def setsockopt(self, *a): pass
        def sendto(self, packet, target):
            sent.append(target)
            assert packet[:6] == bytes([0xFF]) * 6 and len(packet) == 102

    monkeypatch.setattr(actions.socket, "socket", Sock)
    r = actions.wake_on_lan("68:74:37:d4:c0:0b", "172.16.10.101")
    assert sent == [("172.16.10.101", 9), ("172.16.10.255", 9)]
    assert r["summary"] == "sent"


def test_scan_is_disabled_without_enabling(monkeypatch):
    monkeypatch.delenv("DAEDALUS_SCAN_ENABLED", raising=False)
    with pytest.raises(Refused, match="firewall"):
        actions.start_scan("172.16.1.6")


def test_scan_never_in_ping_only_network_and_never_ranges(monkeypatch):
    monkeypatch.setenv("DAEDALUS_SCAN_ENABLED", "yes")
    with pytest.raises(Refused, match="only ping"):
        actions.start_scan("192.168.100.5")
    with pytest.raises(Refused):
        actions.start_scan("172.16.1.0/24")
