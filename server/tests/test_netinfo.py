from __future__ import annotations

import pytest

from sturddle_view import netinfo
from sturddle_view.config import LOOPBACK_HOST, WILDCARD_HOST, Settings

PRIMARY = "192.168.1.5"
VIRTUAL = "172.18.0.1"


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("127.0.0.2", True),
    ("::1", True),
    ("::ffff:127.0.0.1", True),  # IPv4-mapped, as seen on a --host :: bind
    ("localhost", True),
    ("192.168.1.9", False),
    ("testclient", False),
    ("", False),
])
def test_is_loopback(host, expected):
    assert netinfo.is_loopback(host) is expected


def test_is_this_machine_includes_own_interface_addresses(monkeypatch):
    monkeypatch.setattr(netinfo, "reachable_hosts", lambda bind: [PRIMARY, LOOPBACK_HOST])
    assert netinfo.is_this_machine(LOOPBACK_HOST)
    assert netinfo.is_this_machine(PRIMARY)
    assert netinfo.is_this_machine(f"::ffff:{PRIMARY}")  # as seen on a --host :: bind
    assert not netinfo.is_this_machine("192.168.1.9")


def test_reachable_hosts_specific_bind_is_itself():
    assert netinfo.reachable_hosts(PRIMARY) == [PRIMARY]


def test_lan_hosts_drops_loopback_and_puts_default_route_first(monkeypatch):
    monkeypatch.setattr(netinfo, "reachable_hosts", lambda bind: [VIRTUAL, PRIMARY, LOOPBACK_HOST])
    monkeypatch.setattr(netinfo, "primary_lan_ip", lambda: PRIMARY)
    assert netinfo.lan_hosts(WILDCARD_HOST) == [PRIMARY, VIRTUAL]


def test_lan_hosts_without_default_route_keeps_interface_order(monkeypatch):
    monkeypatch.setattr(netinfo, "reachable_hosts", lambda bind: [VIRTUAL, PRIMARY, LOOPBACK_HOST])
    monkeypatch.setattr(netinfo, "primary_lan_ip", lambda: None)
    assert netinfo.lan_hosts(WILDCARD_HOST) == [VIRTUAL, PRIMARY]


@pytest.mark.parametrize("bind", [LOOPBACK_HOST, "localhost"])
def test_lan_hosts_loopback_bind_is_empty(bind):
    assert netinfo.lan_hosts(bind) == []


def test_entry_url_carries_token_through_auth_handshake():
    s = Settings(token="tok", port=9000)
    assert netinfo.entry_url(s, PRIMARY) == f"http://{PRIMARY}:9000/auth?token=tok"


def test_entry_url_without_auth_is_the_root():
    s = Settings(token="tok", port=9000, auth_disabled=True)
    assert netinfo.entry_url(s, PRIMARY) == f"http://{PRIMARY}:9000/"


def test_entry_url_uses_https_with_tls(tmp_path):
    s = Settings(token="tok", port=9000, tls_cert=tmp_path / "c.pem", tls_key=tmp_path / "k.pem")
    assert netinfo.url_scheme(s) == "https"
    assert netinfo.entry_url(s, PRIMARY) == f"https://{PRIMARY}:9000/auth?token=tok"
