"""Passwordless local access: network and host matching (no server needed): pytest -q"""
import socket
from types import SimpleNamespace

import pytest

from app.lockout import host_allowed, ip_in_networks, parse_networks


def test_parse_networks():
    nets = parse_networks("localhost, 192.168.1.0/24; 10.0.0.5")
    assert [str(n) for n in nets] == ["127.0.0.0/8", "::1/128", "192.168.1.0/24", "10.0.0.5/32"]
    assert parse_networks("") == []
    with pytest.raises(ValueError):
        parse_networks("192.168.1.0/33")
    with pytest.raises(ValueError):
        parse_networks("workshop")


@pytest.mark.parametrize("ip,expected", [
    ("127.0.0.1", True), ("::1", True), ("192.168.1.42", True), ("::ffff:192.168.1.42", True),
    ("192.168.2.1", False), ("10.0.0.5", True), ("10.0.0.6", False), ("8.8.8.8", False), ("?", False), ("", False),
])
def test_ip_in_networks(ip, expected):
    assert ip_in_networks(ip, parse_networks("localhost, 192.168.1.0/24, 10.0.0.5")) is expected


@pytest.mark.parametrize("host,expected", [
    ("192.168.1.10:8000", True), ("127.0.0.1", True), ("[::1]:8000", True), ("localhost:8000", True),
    (socket.gethostname() + ":8000", True), (socket.gethostname() + ".local", True),
    ("oven.lan:8000", True),                 # listed in ALLOWED_HOSTS
    ("evil.example.com", False),             # DNS-rebinding style name
    ("", False),
])
def test_host_allowed(host, expected):
    assert host_allowed(host, SimpleNamespace(ALLOWED_HOSTS="oven.lan")) is expected


def test_settings_validation_for_local_access():
    from app import settings as S
    from tests.test_settings import Cfg
    c = type("C", (Cfg,), {"LOCAL_NETWORKS": "", "LOCAL_ACCESS": "control", "ALLOWED_HOSTS": ""})
    assert S.validate(c, {"LOCAL_NETWORKS": " localhost ;192.168.1.0/24 "}) == {"LOCAL_NETWORKS": "localhost, 192.168.1.0/24"}
    assert S.validate(c, {"LOCAL_ACCESS": "VIEW"}) == {"LOCAL_ACCESS": "view"}
    assert S.validate(c, {"ALLOWED_HOSTS": "Oven.LAN, pi4"}) == {"ALLOWED_HOSTS": "oven.lan, pi4"}
    for bad in ({"LOCAL_NETWORKS": "0.0.0.0/0"}, {"LOCAL_NETWORKS": "::/0"}, {"LOCAL_NETWORKS": "the lan"},
                {"LOCAL_ACCESS": "admin"}, {"ALLOWED_HOSTS": "bad host!"}):
        with pytest.raises(S.SettingsError):
            S.validate(c, bad)
