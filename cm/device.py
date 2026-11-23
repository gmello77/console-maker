"""Preconfigurazione di Batocera da device.yaml (lingua, tastiera, fuso orario, Wi-Fi, ScreenScraper).

Come viene applicata (verificato sul codice di Batocera, script S12populateshare): al primo
avvio i file `userdata/system/batocera.conf` ed `es_settings.cfg` vengono creati dai predefiniti
SOLO se non esistono gia'; se li mettiamo noi vengono rispettati ma NON fusi con i predefiniti.
Per questo `merge_conf` parte dal batocera.conf predefinito estratto dalla stessa immagine e
sostituisce soltanto le chiavi scelte.

device.yaml contiene segreti (chiave Wi-Fi, password ScreenScraper): non va nella repo, i valori
non finiscono mai nei log e gli oggetti qui sotto nascondono i segreti nel repr.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class DeviceConfigError(Exception):
    pass


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    key: str = field(default="", repr=False)
    hidden: bool = False


@dataclass(frozen=True)
class DeviceConfig:
    language: str | None = None
    keyboard: str | None = None
    timezone: str | None = None
    hostname: str | None = None
    wifi_country: str | None = None
    networks: tuple[WifiNetwork, ...] = ()
    screenscraper_user: str | None = None
    screenscraper_password: str | None = field(default=None, repr=False)
    extra: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.language or self.keyboard or self.timezone or self.hostname or
                    self.wifi_country or self.networks or self.screenscraper_user or self.extra)

    def summary(self) -> str:
        """Descrizione per la schermata: mai segreti."""
        parts = []
        if self.language:
            parts.append(f"lingua {self.language}")
        if self.timezone:
            parts.append(f"fuso {self.timezone}")
        if self.keyboard:
            parts.append(f"tastiera {self.keyboard}")
        if self.networks:
            parts.append(f"{len(self.networks)} rete/i Wi-Fi")
        if self.screenscraper_user:
            parts.append("ScreenScraper")
        if self.extra:
            parts.append(f"{len(self.extra)} impostazioni extra")
        return ", ".join(parts) or "vuota"

    # ---------------------------------------------------------------- chiavi di batocera.conf
    def conf_overrides(self) -> dict[str, str]:
        o: dict[str, str] = {}
        if self.language:
            o["system.language"] = self.language
        if self.keyboard:
            o["system.kblayout"] = self.keyboard
        if self.timezone:
            o["system.timezone"] = self.timezone
        if self.hostname:
            o["system.hostname"] = self.hostname
        if self.wifi_country:
            o["wifi.country"] = self.wifi_country
        normal = [n for n in self.networks if not n.hidden]
        hidden = [n for n in self.networks if n.hidden]
        if self.networks:
            o["wifi.enabled"] = "1"
        for i, n in enumerate(normal):
            prefix = "wifi" if i == 0 else f"wifi{i + 1}"
            o[f"{prefix}.ssid"] = n.ssid
            o[f"{prefix}.key"] = escape_conf_value(n.key)
        if hidden:                                   # Batocera ne prevede una sola
            o["wifi.hidden.ssid"] = hidden[0].ssid
            o["wifi.hidden.key"] = escape_conf_value(hidden[0].key)
        o.update(dict(self.extra))
        return o

    def es_settings_xml(self) -> str | None:
        """es_settings.cfg con le sole credenziali ScreenScraper (EmulationStation scrive solo
        i valori non predefiniti, quindi un file cosi' e' del formato che userebbe lui)."""
        if not self.screenscraper_user:
            return None
        values = {"ScreenScraperUser": self.screenscraper_user}
        if self.screenscraper_password:
            values["ScreenScraperPass"] = self.screenscraper_password
        root = ET.Element("config")
        for name, value in values.items():
            ET.SubElement(root, "string", name=name, value=value)
        ET.indent(root, space="\t")
        return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# ---------------------------------------------------------------- validazione
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]]+$")
_LANG_RE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}$")
_KB_RE = re.compile(r"^[a-z]{2,10}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_COUNTRY_RE = re.compile(r"^([A-Z]{2}|00)$")


def escape_conf_value(value: str) -> str:
    """Batocera vuole il backslash davanti a `#`, `;` e `$` (documentato per wifi.key)."""
    return re.sub(r"([#;$])", r"\\\1", value)


def _text(value: object, where: str, *, max_len: int = 200, allow_empty: bool = False) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise DeviceConfigError(f"{where}: deve essere un testo")
    s = str(value)
    if _CTRL.search(s):
        # un a-capo permetterebbe di iniettare altre righe in batocera.conf
        raise DeviceConfigError(f"{where}: non puo' contenere a-capo o caratteri di controllo")
    if not s and not allow_empty:
        raise DeviceConfigError(f"{where}: e' vuoto")
    if len(s) > max_len:
        raise DeviceConfigError(f"{where}: troppo lungo (max {max_len} caratteri)")
    return s


def _match(value: object, rx: re.Pattern, where: str, hint: str) -> str:
    s = _text(value, where, max_len=64)
    if not rx.match(s):
        raise DeviceConfigError(f"{where}: {s!r} non e' valido ({hint})")
    return s


def load_device(path: Path) -> DeviceConfig | None:
    """None se il file non esiste. Solleva DeviceConfigError se non e' valido."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except yaml.YAMLError as e:
        raise DeviceConfigError(f"{path.name}: YAML non valido: {e}")
    return parse_device(data)


def parse_device(data: object) -> DeviceConfig:
    if data is None:
        return DeviceConfig()
    if not isinstance(data, dict):
        raise DeviceConfigError("device.yaml: deve essere un elenco di impostazioni")
    known = {"language", "keyboard", "timezone", "hostname", "wifi", "screenscraper", "extra"}
    unknown = sorted(set(map(str, data)) - known)
    if unknown:
        raise DeviceConfigError(f"device.yaml: chiavi sconosciute: {', '.join(unknown)} "
                                f"(valide: {', '.join(sorted(known))})")
    warnings: list[str] = []
    kw: dict = {}
    if data.get("language") is not None:
        kw["language"] = _match(data["language"], _LANG_RE, "language", "esempio it_IT")
    if data.get("keyboard") is not None:
        kw["keyboard"] = _match(data["keyboard"], _KB_RE, "keyboard", "esempio it")
    if data.get("hostname") is not None:
        kw["hostname"] = _match(data["hostname"], _HOST_RE, "hostname",
                                "lettere, cifre e trattini")
    if data.get("timezone") is not None:
        tz = _text(data["timezone"], "timezone", max_len=64)
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise DeviceConfigError(f"timezone: {tz!r} non esiste (esempio Europe/Rome)")
        kw["timezone"] = tz

    wifi = data.get("wifi")
    if wifi is not None:
        if not isinstance(wifi, dict):
            raise DeviceConfigError("wifi: deve contenere 'country' e/o 'networks'")
        if wifi.get("country") is not None:
            kw["wifi_country"] = _match(wifi["country"], _COUNTRY_RE, "wifi.country",
                                        "due lettere maiuscole, es. IT, oppure 00")
        nets = wifi.get("networks") or []
        if not isinstance(nets, list):
            raise DeviceConfigError("wifi.networks: deve essere un elenco")
        parsed = []
        for i, n in enumerate(nets, 1):
            where = f"wifi.networks[{i}]"
            if not isinstance(n, dict) or "ssid" not in n:
                raise DeviceConfigError(f"{where}: servono almeno 'ssid' (e 'key')")
            ssid = _text(n["ssid"], f"{where}.ssid", max_len=32)
            key = _text(n.get("key", ""), f"{where}.key", max_len=64, allow_empty=True)
            if key and not (8 <= len(key) <= 63 or re.fullmatch(r"[0-9A-Fa-f]{64}", key)):
                raise DeviceConfigError(f"{where}.key: una chiave WPA ha 8-63 caratteri "
                                        "(o 64 cifre esadecimali)")
            if re.search(r"[#;$]", ssid):
                warnings.append(f"{where}: l'SSID contiene # ; o $ e Batocera non documenta "
                                "come trattarli: se non si collega, e' questo")
            parsed.append(WifiNetwork(ssid, key, bool(n.get("hidden", False))))
        if sum(1 for n in parsed if n.hidden) > 1:
            warnings.append("wifi: Batocera supporta una sola rete nascosta: si usa la prima")
        kw["networks"] = tuple(parsed)

    ss = data.get("screenscraper")
    if ss is not None:
        if not isinstance(ss, dict) or not ss.get("user"):
            raise DeviceConfigError("screenscraper: servono 'user' e 'password'")
        kw["screenscraper_user"] = _text(ss["user"], "screenscraper.user", max_len=100)
        if ss.get("password") is not None:
            kw["screenscraper_password"] = _text(ss["password"], "screenscraper.password",
                                                 max_len=200)
        else:
            warnings.append("screenscraper: manca 'password': lo scraper la chiedera' al primo uso")

    extra = data.get("extra")
    if extra is not None:
        if not isinstance(extra, dict):
            raise DeviceConfigError("extra: deve essere un elenco chiave: valore di batocera.conf")
        pairs = []
        for k, v in extra.items():
            k = str(k)
            if not _KEY_RE.match(k):
                raise DeviceConfigError(f"extra: chiave non valida: {k!r}")
            pairs.append((k, escape_conf_value(_text(v, f"extra.{k}", allow_empty=True))))
        kw["extra"] = tuple(pairs)
    return DeviceConfig(warnings=tuple(warnings), **kw)


# ---------------------------------------------------------------- fusione con i predefiniti
def merge_es_settings(default_xml: str, ours_xml: str) -> str:
    """Unisce i nodi <string> di `ours_xml` in un es_settings.cfg predefinito (se esiste)."""
    root = ET.fromstring(default_xml)
    for node in ET.fromstring(ours_xml).findall("string"):
        for old in [n for n in root.findall("string") if n.get("name") == node.get("name")]:
            root.remove(old)
        root.append(node)
    ET.indent(root, space="\t")
    return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def merge_conf(default_text: str, overrides: dict[str, str]) -> str:
    """Sostituisce/aggiunge `overrides` nel batocera.conf predefinito, conservando il resto.

    Per ogni chiave: la prima riga attiva (`chiave=...`) viene sostituita; le altre righe attive
    della stessa chiave vengono tolte; se non c'e' una riga attiva si attiva la prima riga
    commentata (`#chiave=...`) cosi' il valore resta accanto alla sua documentazione; altrimenti
    si accoda in fondo."""
    lines = default_text.splitlines()
    for key, value in overrides.items():
        new = f"{key}={value}"
        active = re.compile(rf"^{re.escape(key)}\s*=")
        commented = re.compile(rf"^#\s*{re.escape(key)}\s*=")
        idx = next((i for i, ln in enumerate(lines) if active.match(ln)), None)
        if idx is None:
            idx = next((i for i, ln in enumerate(lines) if commented.match(ln)), None)
        if idx is None:
            lines.append(new)
            continue
        lines[idx] = new
        lines = [ln for i, ln in enumerate(lines) if i <= idx or not active.match(ln)]
    return "\n".join(lines) + "\n"
