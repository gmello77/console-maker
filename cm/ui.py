"""Schermata unica con menu a scelta (Textual)."""
from __future__ import annotations

import time
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Button, Footer, Header, Input, Label, ProgressBar,
                             RadioButton, RadioSet, RichLog, SelectionList, Static)
from textual.widgets.selection_list import Selection

from . import archive, auth, config, pipeline
from .device import DeviceConfig, DeviceConfigError, load_device
from .distro import TARGETS
from .login_screen import LoginScreen
from .picker import GamePicker
from .progress import Speedometer, describe, human_bytes, human_eta
from .roms import (RomsConfig, RomsConfigError, System, applicable_bios, estimate_sizes,
                   expand_system, load_roms)

DISK_CHOICES = (16, 32, 64, 128, 256, 512)
IMAGE_BASE_GB = 8.9   # boot 8 GB + margine SHARE: stima, il valore esatto lo legge la pipeline
OPTIONS = (
    ("fit_disk", "Se non ci stanno, togli i giochi meno importanti (vedi priority)"),
    ("skip_bios", "Non includere i file BIOS di roms.yaml"),
    ("skip_device", "Non applicare device.yaml (lingua, Wi-Fi, ScreenScraper)"),
    ("verify_md5", "Verifica md5 dei file scaricati (immagine, ROM, BIOS)"),
    ("keep_cache", "Tieni i download in cache (cache/)"),
    ("create_all_dirs", "Crea le cartelle vuote di tutti i sistemi di roms.yaml"),
)


class ConsoleMakerApp(App):
    TITLE = "console-maker"
    SUB_TITLE = "immagini Batocera con ROM precaricate"
    BINDINGS = [("q", "quit", "Esci"), ("a", "toggle_all", "Tutti/nessun sistema")]
    CSS = """
    #menu { height: 3fr; min-height: 6; }
    .col { width: 1fr; height: 100%; padding: 0 1; }
    .col > Label { text-style: bold; margin-top: 1; }
    SelectionList { height: auto; max-height: 12; }
    #summary { padding: 0 2; height: auto; }
    #bar, #overall { margin: 0 2; }
    #barlabel, #overalllabel { margin: 0 2; }
    #log { height: 2fr; min-height: 5; margin: 0 1; border: round $primary; }
    #start { margin: 0 2; }
    """

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.root = root or pipeline.ROOT
        self.cfg_path = self.root / "config.yaml"
        self.saved = config.load(self.cfg_path)
        self.roms: RomsConfig | None = None
        self.selections = config.load_selections(self.root / "selections.yaml")
        self.expanded: dict[str, System] = {}      # sistemi con gli item gia' risolti in URL
        self.item_files: dict[str, list[archive.ArchiveFile]] = {}
        self.sizes: dict[str, int | None] = {}
        self.loaded = False
        self.auth = auth.load_auth()
        self.running = False
        self._last_tick = 0.0
        self._last_label = ""
        self._last_overall = 0.0
        self.speedo = Speedometer()            # file in corso
        self.overall_speedo = Speedometer()    # totale ROM + BIOS
        self.bios_expanded: dict[str, System] = {}
        self.device: DeviceConfig | None = None
        self.device_error: str | None = None

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="menu"):
            with VerticalScroll(classes="col"):
                yield Label("Dispositivo")
                yield RadioSet(*[RadioButton(t.label, id=f"dev_{t.key}",
                                             value=t.key == self.saved["device"])
                                 for t in TARGETS], id="device")
                yield Label("Disco di destinazione")
                yield RadioSet(*[RadioButton(f"{g} GB", id=f"disk_{g}",
                                             value=g == self.saved["disk_gb"])
                                 for g in DISK_CHOICES], id="disk")
                yield Input(placeholder="oppure un altro valore in GB", id="disk_other",
                            type="number")
            with VerticalScroll(classes="col"):
                yield Label("Console / emulatori  (spazio = scegli, a = tutti)")
                yield SelectionList(id="systems")
                yield Button("Scegli i giochi del sistema evidenziato...", id="pick")
                yield Label("archive.org")
                yield Static(id="authstatus")
                yield Button("Login archive.org...", id="open_login")
                yield Label("Opzioni")
                yield SelectionList(*[Selection(text, key, key in self.saved["options"])
                                      for key, text in OPTIONS], id="options")
        yield Static(id="summary")
        yield Button("Avvia", id="start", variant="success", disabled=True)
        yield Label("", id="barlabel")
        yield ProgressBar(id="bar", total=100, show_eta=False)
        yield Label("", id="overalllabel")
        yield ProgressBar(id="overall", total=100, show_eta=False)
        yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def show_auth_status(self) -> None:
        self.query_one("#authstatus", Static).update(
            "collegato: gli item riservati sono accessibili" if self.auth
            else "non collegato: gli item ad accesso riservato non si scaricano")

    def on_mount(self) -> None:
        self.show_auth_status()
        log = self.query_one("#log", RichLog)
        try:
            self.device = load_device(self.root / "device.yaml")
        except DeviceConfigError as e:
            self.device_error = str(e)
            log.write(f"ERRORE: {e}")
        else:
            for w in (self.device.warnings if self.device else ()):
                log.write(f"avviso: {w}")
        try:
            self.roms = load_roms(self.root / "roms.yaml")
        except RomsConfigError as e:
            log.write(f"ERRORE: {e}")
            self.refresh_summary()
            return
        for w in self.roms.warnings:
            log.write(f"avviso: {w}")
        self.refresh_summary()
        self.load_catalog()

    @work(thread=True, exclusive=True, group="catalog")
    def load_catalog(self) -> None:
        """Risolve gli `item:` in elenchi di file e calcola le dimensioni (rete)."""
        assert self.roms
        cache = self.root / "cache" / "meta"
        expanded: dict[str, System] = {}
        item_files: dict[str, list[archive.ArchiveFile]] = {}
        notes: list[str] = []
        for s in self.roms.systems.values():
            if s.item:
                try:
                    allf = archive.list_files(s.item, cache)
                except Exception as e:  # rete, item inesistente...: lo si dice, non si nasconde
                    notes.append(f"ERRORE {s.key}: item {s.item}: {e}")
                    expanded[s.key] = s
                    continue
                item_files[s.key] = archive.select_files(
                    allf, include=s.include, exclude=s.exclude)
                s, warns = expand_system(s, allf, self.selections.get(s.key),
                                         logged_in=self.auth is not None)
                notes += [f"avviso: {w}" for w in warns]
            expanded[s.key] = s
        bios: dict[str, System] = {}
        for b in self.roms.bios.values():
            if b.item:
                try:
                    b, warns = expand_system(b, archive.list_files(b.item, cache),
                                             logged_in=self.auth is not None)
                    notes += [f"avviso: {w}" for w in warns]
                except Exception as e:
                    notes.append(f"ERRORE bios {b.key}: item {b.item}: {e}")
            bios[b.key] = b
        allsys = [*expanded.values(), *bios.values()]
        urls = [u for s in allsys for u in s.urls]
        known = {u: sz for s in allsys for u, sz in s.sizes.items()}
        sizes = {**estimate_sizes([u for u in urls if u not in known], auth=self.auth), **known}
        self.call_from_thread(self._catalog_ready, expanded, item_files, sizes, notes, bios)

    def _catalog_ready(self, expanded: dict[str, System],
                       item_files: dict[str, list[archive.ArchiveFile]],
                       sizes: dict[str, int | None], notes: list[str],
                       bios: dict[str, System] | None = None) -> None:
        log = self.query_one("#log", RichLog)
        for n in notes:
            log.write(n)
        self.expanded, self.item_files, self.sizes = expanded, item_files, sizes
        self.bios_expanded = bios or {}
        sl = self.query_one("#systems", SelectionList)
        # al ricaricamento si mantiene la scelta corrente, alla prima volta quella salvata
        keep = set(sl.selected) if self.loaded else set(self.saved["systems"])
        sl.clear_options()
        for s in expanded.values():
            if not s.urls:
                continue
            total = sum(sizes.get(u) or 0 for u in s.urls)
            unknown = any(sizes.get(u) is None for u in s.urls)
            label = f"{s.name}  ({len(s.urls)} file"
            label += f", {total / 1e9:.1f} GB+)" if unknown else f", {total / 1e9:.1f} GB)"
            sl.add_option(Selection(label, s.key, not keep or s.key in keep))
        self.loaded = True
        if sl.option_count == 0:
            log.write("roms.yaml non contiene nessun link: compila 'urls' o 'item'.")
        self.query_one("#start", Button).disabled = (
            sl.option_count == 0 or self.running or self.device_error is not None)
        self.refresh_summary()

    # ------------------------------------------------------------ stato
    def _disk_gb(self) -> float | None:
        other = self.query_one("#disk_other", Input).value.strip()
        if other:
            try:
                v = float(other)
                return v if v > 0 else None
            except ValueError:
                return None
        btn = self.query_one("#disk", RadioSet).pressed_button
        return float(btn.id.removeprefix("disk_")) if btn and btn.id else None

    def _device_key(self) -> str:
        btn = self.query_one("#device", RadioSet).pressed_button
        return btn.id.removeprefix("dev_") if btn and btn.id else TARGETS[0].key

    def _selected_systems(self) -> list[str]:
        return list(self.query_one("#systems", SelectionList).selected)

    def refresh_summary(self) -> None:
        lines = []
        disk = self._disk_gb()
        if self.roms:
            chosen = [self.expanded[k] for k in self._selected_systems() if k in self.expanded]
            urls = [u for s in chosen for u in s.urls]
            known = sum(self.sizes.get(u) or 0 for u in urls)
            unknown = sum(1 for u in urls if self.sizes.get(u) is None)
            txt = f"{len(chosen)} sistemi, {len(urls)} file"
            bios_urls = [u for b in self.selected_bios() for u in b.urls]
            if bios_urls:
                bios_bytes = sum(self.sizes.get(u) or 0 for u in bios_urls)
                known += bios_bytes
                txt += f" + {len(bios_urls)} file BIOS ({human_bytes(bios_bytes)})"
            if not self.loaded:
                txt += " · carico gli elenchi da archive.org..."
            else:
                txt += f" · ROM scaricate circa {known / 1e9:.1f} GB"
                if unknown:
                    txt += f" (+{unknown} file di dimensione ignota)"
            lines.append(txt)
            device = self.selected_device()
            if device:
                lines.append(f"Preconfigurazione (device.yaml): {device.summary()}")
            if disk:
                usable = pipeline.disk_capacity_bytes(disk) / 1e9
                lines.append(f"Disco {disk:g} GB (utilizzabili circa {usable:.1f} GB): "
                             "l'immagine Batocera da sola occupa circa 8,5 GB.")
                room = usable - IMAGE_BASE_GB
                if self.loaded and known / 1e9 > room:
                    if "fit_disk" in self.query_one("#options", SelectionList).selected:
                        lines.append(f"Non ci sta (ROM {known / 1e9:.1f} GB, spazio circa "
                                     f"{max(room, 0):.1f} GB): all'avvio verranno tolti i giochi "
                                     "meno importanti.")
                    else:
                        lines.append(f"ATTENZIONE: non ci sta (ROM {known / 1e9:.1f} GB, spazio "
                                     f"circa {max(room, 0):.1f} GB). Riduci la selezione o attiva "
                                     "l'adattamento automatico.")
        else:
            lines.append("Nessun roms.yaml valido.")
        self.query_one("#summary", Static).update("\n".join(lines))

    # ------------------------------------------------------------ eventi
    def on_radio_set_changed(self, _: RadioSet.Changed) -> None:
        self.refresh_summary()

    def on_input_changed(self, _: Input.Changed) -> None:
        self.refresh_summary()

    def on_selection_list_selected_changed(self, _: SelectionList.SelectedChanged) -> None:
        self.refresh_summary()

    def action_toggle_all(self) -> None:
        sl = self.query_one("#systems", SelectionList)
        if len(sl.selected) == sl.option_count:
            sl.deselect_all()
        else:
            sl.select_all()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.running:
            return
        if event.button.id == "pick":
            self.open_picker()
            return
        if event.button.id == "open_login":
            self.open_login()
            return
        if event.button.id != "start":
            return
        disk = self._disk_gb()
        keys = self._selected_systems()
        log = self.query_one("#log", RichLog)
        if not self.roms or not keys:
            log.write("Scegli almeno una console.")
            return
        if not disk:
            log.write("Scegli la dimensione del disco.")
            return
        opts = list(self.query_one("#options", SelectionList).selected)
        conns = self._connections()
        config.save(self.cfg_path, {"device": self._device_key(), "connections": conns,
                                    "disk_gb": int(disk) if disk == int(disk) else disk,
                                    "systems": keys, "options": opts})
        settings = pipeline.Settings(
            target_key=self._device_key(), disk_gb=disk,
            systems=[self.expanded[k] for k in keys],
            verify_md5="verify_md5" in opts, keep_cache="keep_cache" in opts,
            fit_to_disk="fit_disk" in opts, sizes=dict(self.sizes), auth=self.auth,
            connections=conns, device=self.selected_device(),
            bios=[] if "skip_bios" in opts else list(self.bios_expanded.values()),
            create_all_dirs="create_all_dirs" in opts,
            all_system_keys=tuple(self.roms.systems), root=self.root)
        self.reset_progress()
        self.set_running(True)
        self.run_pipeline(settings)

    def _connections(self) -> int:
        """Connessioni per file dal config.yaml, limitate a 1-16 (valori strani = predefinito)."""
        v = self.saved.get("connections")
        return v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 16 else 4

    def selected_device(self) -> DeviceConfig | None:
        """device.yaml da applicare (None se manca, e' vuoto o e' stato escluso dalle opzioni)."""
        if self.device is None or self.device.is_empty:
            return None
        if "skip_device" in self.query_one("#options", SelectionList).selected:
            return None
        return self.device

    def selected_bios(self) -> list[System]:
        """BIOS che finirebbero nell'immagine con la scelta attuale (come fa la pipeline)."""
        if "skip_bios" in self.query_one("#options", SelectionList).selected:
            return []
        return applicable_bios(list(self.bios_expanded.values()), set(self._selected_systems()))

    def reset_progress(self) -> None:
        self.speedo, self.overall_speedo = Speedometer(), Speedometer()
        for label in ("#barlabel", "#overalllabel"):
            self.query_one(label, Label).update("")
        for bar in ("#bar", "#overall"):
            self.query_one(bar, ProgressBar).update(total=100, progress=0)

    def open_login(self) -> None:
        def done(changed: bool | None) -> None:
            if not changed:
                return
            self.auth = auth.load_auth()
            self.show_auth_status()
            self.query_one("#log", RichLog).write(
                "Login archive.org eseguito." if self.auth else "Disconnesso da archive.org.")
            if self.roms:
                self.load_catalog()     # riesamina dimensioni e avvisi con il nuovo stato

        self.push_screen(LoginScreen(self.auth is not None), done)

    def open_picker(self) -> None:
        sl = self.query_one("#systems", SelectionList)
        idx = sl.highlighted
        if idx is None:
            self.notify("Evidenzia prima un sistema nella lista.", severity="warning")
            return
        key = sl.get_option_at_index(idx).value
        if key not in self.item_files:
            self.notify("Questo sistema non usa 'item:' in roms.yaml: i suoi file sono "
                        "gia' elencati uno per uno.", severity="warning")
            return
        system = self.roms.systems[key]  # type: ignore[union-attr]
        games = archive.group_games(self.item_files[key])
        current = self.selections.get(key, list(system.files) if system.files is not None else None)

        def done(names: list[str] | None) -> None:
            if names is None:
                return
            self.selections[key] = names
            config.save_selections(self.root / "selections.yaml", self.selections)
            self.query_one("#log", RichLog).write(f"{system.name}: {len(names)} file scelti")
            self.load_catalog()

        self.push_screen(GamePicker(system.name, games, current), done)

    def set_running(self, value: bool) -> None:
        self.running = value
        self.query_one("#start", Button).disabled = value
        for w in ("#device", "#disk", "#disk_other", "#systems", "#options", "#pick", "#open_login"):
            self.query_one(w).disabled = value

    # ------------------------------------------------------------ esecuzione
    @work(thread=True, exclusive=True)
    def run_pipeline(self, settings: pipeline.Settings) -> None:
        def log(msg: str) -> None:
            self.call_from_thread(self.query_one("#log", RichLog).write, msg)

        def progress(label: str, done: int, total: int) -> None:
            now = time.monotonic()
            if label == self._last_label and now - self._last_tick < 0.2 and done != total:
                return
            self._last_label, self._last_tick = label, now
            self.call_from_thread(self._show_progress, label, done, total)

        def overall(fd: int, ft: int, bd: int, bt: int) -> None:
            now = time.monotonic()
            if fd < ft and now - self._last_overall < 0.2:
                return
            self._last_overall = now
            self.call_from_thread(self._show_overall, fd, ft, bd, bt)

        try:
            final = pipeline.run(settings, log, progress, overall)
        except Exception as e:  # mostrato all'utente, non nascosto
            log(f"ERRORE: {e}")
            self.call_from_thread(self.notify, str(e), title="Errore", severity="error")
        else:
            self.call_from_thread(
                self.notify, f"Immagine pronta: {final}", title="Fatto", timeout=15)
            log("Ora aprila con balenaEtcher e scrivila sulla microSD/disco.")
            log("Al primo avvio lancia lo scraper di Batocera (START > Scraper) per copertine e video.")
        finally:
            self.call_from_thread(self.set_running, False)

    def _show_progress(self, label: str, done: int, total: int,
                       now: float | None = None) -> None:
        speed = self.speedo.update(label, done, time.monotonic() if now is None else now)
        self.query_one("#barlabel", Label).update(
            describe(label, done, total, speed, self.speedo.eta(done, total)))
        bar = self.query_one("#bar", ProgressBar)
        if total > 0:
            bar.update(total=total, progress=done)
        else:
            bar.update(total=None)

    def _show_overall(self, files_done: int, files_total: int, bytes_done: int,
                      bytes_total: int, now: float | None = None) -> None:
        text = f"Totale ROM e BIOS: {files_done}/{files_total} file"
        bar = self.query_one("#overall", ProgressBar)
        if bytes_total > 0:
            bytes_done = min(bytes_done, bytes_total)     # le stime possono sbagliare di poco
            speed = self.overall_speedo.update(
                "overall", bytes_done, time.monotonic() if now is None else now)
            text += f"  ·  {human_bytes(bytes_done)} / {human_bytes(bytes_total)}"
            if speed > 0:
                text += (f"  ·  {human_bytes(speed)}/s  ·  ancora "
                         f"{human_eta(self.overall_speedo.eta(bytes_done, bytes_total))}")
            bar.update(total=bytes_total, progress=bytes_done)
        elif files_total > 0:                              # dimensioni ignote: si contano i file
            bar.update(total=files_total, progress=files_done)
        self.query_one("#overalllabel", Label).update(text)
