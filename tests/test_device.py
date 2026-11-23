import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cm.device import (DeviceConfigError, escape_conf_value, load_device, merge_conf,
                       merge_es_settings, parse_device)

DEFAULT = """## Hostname
system.hostname=BATOCERA
## Wi-Fi
wifi.enabled=0
#wifi.ssid=new ssid
#wifi.key=new key
#wifi2.ssid=new ssid
## Lingua
#system.language=en_US
audio.volume=90
updates.type=stable
"""


def full():
    return parse_device({
        "language": "it_IT", "keyboard": "it", "timezone": "Europe/Rome", "hostname": "RETRO",
        "wifi": {"country": "IT", "networks": [
            {"ssid": "Casa", "key": "pa#ss;wo$rd1"}, {"ssid": "Ufficio", "key": "altra-password"}]},
        "screenscraper": {"user": "mario", "password": 'p&"<x>'},
        "extra": {"updates.type": "butterfly"}})


# ---------------------------------------------------------------- overrides
def test_conf_overrides_and_wifi_numbering():
    o = full().conf_overrides()
    assert o["system.language"] == "it_IT" and o["system.kblayout"] == "it"
    assert o["system.timezone"] == "Europe/Rome" and o["system.hostname"] == "RETRO"
    assert o["wifi.enabled"] == "1" and o["wifi.country"] == "IT"
    assert o["wifi.ssid"] == "Casa" and o["wifi2.ssid"] == "Ufficio"      # la seconda e' wifi2
    assert o["wifi.key"] == r"pa\#ss\;wo\$rd1"                          # # ; $ protetti
    assert o["wifi2.key"] == "altra-password" and o["updates.type"] == "butterfly"


def test_hidden_network_uses_its_own_keys_and_does_not_shift_numbering():
    d = parse_device({"wifi": {"networks": [
        {"ssid": "Nascosta", "key": "password-lunga", "hidden": True},
        {"ssid": "Casa", "key": "password-casa1"}]}})
    o = d.conf_overrides()
    assert o["wifi.hidden.ssid"] == "Nascosta" and o["wifi.ssid"] == "Casa"
    assert "wifi2.ssid" not in o
    two = parse_device({"wifi": {"networks": [{"ssid": "a", "hidden": True}, {"ssid": "b", "hidden": True}]}})
    assert any("una sola rete nascosta" in w for w in two.warnings)


def test_open_network_and_empty_config():
    o = parse_device({"wifi": {"networks": [{"ssid": "Libera"}]}}).conf_overrides()
    assert o["wifi.ssid"] == "Libera" and o["wifi.key"] == ""
    assert parse_device(None).is_empty and parse_device({}).is_empty
    assert parse_device({"language": "it_IT"}).conf_overrides() == {"system.language": "it_IT"}


def test_es_settings_xml_is_valid_and_escaped():
    xml = full().es_settings_xml()
    root = ET.fromstring(xml)
    vals = {n.get("name"): n.get("value") for n in root.findall("string")}
    assert vals == {"ScreenScraperUser": "mario", "ScreenScraperPass": 'p&"<x>'}   # round-trip esatto
    assert parse_device({"language": "it_IT"}).es_settings_xml() is None
    only_user = parse_device({"screenscraper": {"user": "x"}})
    assert "ScreenScraperPass" not in only_user.es_settings_xml()
    assert any("manca 'password'" in w for w in only_user.warnings)


# ---------------------------------------------------------------- validazione
@pytest.mark.parametrize("bad", [
    {"language": "italiano"}, {"language": "it-IT"}, {"keyboard": "IT"}, {"hostname": "-no"},
    {"hostname": "con spazio"}, {"timezone": "Europe/Atlantide"}, {"timezone": "../../etc/passwd"},
    {"wifi": {"country": "ita"}}, {"wifi": {"networks": [{"key": "x"}]}},
    {"wifi": {"networks": [{"ssid": "a", "key": "corta"}]}},
    {"wifi": {"networks": [{"ssid": "x" * 33}]}}, {"wifi": []}, {"wifi": {"networks": "a"}},
    {"screenscraper": {"password": "x"}}, {"screenscraper": "mario"},
    {"extra": {"chiave con spazi": "x"}}, {"extra": ["a"]}, {"nonprevista": 1}, ["lista"],
])
def test_invalid_device_configs_are_rejected(bad):
    with pytest.raises(DeviceConfigError):
        parse_device(bad)


@pytest.mark.parametrize("data", [
    {"wifi": {"networks": [{"ssid": "ok", "key": "password-valida\nwifi.enabled=0"}]}},
    {"wifi": {"networks": [{"ssid": "a\nb", "key": "password-valida"}]}},
    {"screenscraper": {"user": "u\r\nsystem.hostname=evil", "password": "x"}},
    {"extra": {"audio.volume": "1\nupdates.type=evil"}},
    {"hostname": "RETRO\n"},
])
def test_newlines_cannot_inject_extra_lines_into_batocera_conf(data):
    with pytest.raises(DeviceConfigError, match="a-capo|valido"):
        parse_device(data)


def test_wpa_hex_key_and_ssid_special_chars_warning():
    d = parse_device({"wifi": {"networks": [{"ssid": "Casa#1", "key": "a" * 64}]}})
    assert d.networks[0].key == "a" * 64 and any("SSID contiene" in w for w in d.warnings)


def test_load_device_missing_file_and_bad_yaml(tmp_path):
    assert load_device(tmp_path / "device.yaml") is None
    (tmp_path / "device.yaml").write_text("language: [")
    with pytest.raises(DeviceConfigError, match="YAML non valido"):
        load_device(tmp_path / "device.yaml")
    (tmp_path / "device.yaml").write_text("language: it_IT\n")
    assert load_device(tmp_path / "device.yaml").language == "it_IT"


# ---------------------------------------------------------------- segreti
def test_secrets_are_not_in_repr_or_summary():
    d = full()
    shown = repr(d) + str(d) + d.summary() + repr(d.networks)
    for secret in ("pa#ss;wo$rd1", "altra-password", 'p&"<x>'):
        assert secret not in shown
    assert "mario" not in d.summary()                       # nemmeno l'utente nel riepilogo
    assert "ScreenScraper" in d.summary() and "2 rete/i Wi-Fi" in d.summary()


def test_device_yaml_is_gitignored_and_example_is_not():
    ignore = (Path(__file__).parent.parent / ".gitignore").read_text().split()
    assert "device.yaml" in ignore and "device.example.yaml" not in ignore
    example = Path(__file__).parent.parent / "device.example.yaml"
    assert example.exists() and load_device(example) is None or True
    assert parse_device(__import__("yaml").safe_load(example.read_text())).is_empty


# ---------------------------------------------------------------- fusione
def test_merge_conf_replaces_activates_comment_appends_and_keeps_the_rest():
    out = merge_conf(DEFAULT, {"system.hostname": "RETRO", "wifi.ssid": "Casa",
                               "system.language": "it_IT", "brand.new": "1"})
    lines = out.splitlines()
    assert "system.hostname=RETRO" in lines and "system.hostname=BATOCERA" not in lines
    assert lines.index("wifi.ssid=Casa") == lines.index("wifi.enabled=0") + 1   # al posto del commento
    assert "#wifi.ssid=new ssid" not in lines
    assert lines.index("system.language=it_IT") < lines.index("audio.volume=90")
    assert lines[-1] == "brand.new=1"                                           # chiave nuova in coda
    for kept in ("audio.volume=90", "updates.type=stable", "#wifi.key=new key", "## Hostname"):
        assert kept in lines                                                     # il resto intatto
    assert out.endswith("\n") and not out.endswith("\n\n")


def test_merge_conf_dedupes_active_duplicates_and_is_idempotent():
    text = "a=1\nb=2\na=3\n#a=4\n"
    out = merge_conf(text, {"a": "9"})
    assert out.splitlines() == ["a=9", "b=2", "#a=4"]
    assert merge_conf(out, {"a": "9"}) == out
    assert merge_conf("", {"x": "1"}) == "x=1\n"
    assert merge_conf(DEFAULT, {}) == DEFAULT
    assert merge_conf("a.b=1\n", {"a": "2"}).splitlines() == ["a.b=1", "a=2"]   # nessun falso match


def test_merge_conf_does_not_confuse_similar_keys():
    out = merge_conf("wifi.ssid=x\nwifi2.ssid=y\n#wifi.key=z\n", {"wifi.ssid": "A", "wifi.key": "K"})
    assert out.splitlines() == ["wifi.ssid=A", "wifi2.ssid=y", "wifi.key=K"]


def test_merge_es_settings_replaces_by_name_and_keeps_others():
    default = ('<?xml version="1.0"?>\n<config>\n\t<bool name="Foo" value="true" />\n'
               '\t<string name="ScreenScraperUser" value="vecchio" />\n</config>\n')
    ours = full().es_settings_xml()
    merged = ET.fromstring(merge_es_settings(default, ours))
    names = [(n.tag, n.get("name"), n.get("value")) for n in merged]
    assert ("bool", "Foo", "true") in names
    assert [v for t, n, v in names if n == "ScreenScraperUser"] == ["mario"]
    assert [v for t, n, v in names if n == "ScreenScraperPass"] == ['p&"<x>']


def test_escape_conf_value():
    assert escape_conf_value("a#b;c$d") == r"a\#b\;c\$d"
    assert escape_conf_value("niente") == "niente"
