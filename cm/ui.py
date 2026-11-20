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

from . import config, pipeline
from .distro import TARGETS
from .roms import RomsConfig, RomsConfigError, estimate_sizes, load_roms

DISK_CHOICES = (16, 32, 64, 128, 256, 512)
OPTIONS = (
    ("verify_md5", "Verifica md5 dell'immagine Batocera"),
    ("keep_cache", "Tieni i download in cache (cache/)"),
    ("create_all_dirs", "Crea le cartelle vuote di tutti i sistemi di roms.yaml"),
)


class ConsoleMakerApp(App):
    TITLE = "console-maker"
    SUB_TITLE = "immagini Batocera con ROM precaricate"
    BINDINGS = [("q", "quit", "Esci"), ("a", "toggle_all", "Tutti/nessun sistema")]
    CSS = """
    #menu { height: auto; max-height: 60%; }
    .col { width: 1fr; height: auto; padding: 0 1; }
    .col > Label { text-style: bold; margin-top: 1; }
    SelectionList { height: auto; max-height: 12; }
    #summary { padding: 0 2; height: auto; }
    #bar { margin: 0 2; }
    #barlabel { margin: 0 2; }
    #log { height: 1fr; margin: 0 1; border: round $primary; }
    #start { margin: 1 2; }
    """

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.root = root or pipeline.ROOT
        self.cfg_path = self.root / "config.yaml"
        self.saved = config.load(self.cfg_path)
        self.roms: RomsConfig | None = None
        self.sizes: dict[str, int | None] = {}
        self.running = False
        self._last_tick = 0.0
        self._last_label = ""

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
                yield Label("Opzioni")
                yield SelectionList(*[Selection(text, key, key in self.saved["options"])
                                      for key, text in OPTIONS], id="options")
        yield Static(id="summary")
        yield Button("Avvia", id="start", variant="success")
        yield Label("", id="barlabel")
        yield ProgressBar(id="bar", total=100, show_eta=False)
        yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        try:
            self.roms = load_roms(self.root / "roms.yaml")
        except RomsConfigError as e:
            log.write(f"ERRORE: {e}")
            self.query_one("#start", Button).disabled = True
            self.refresh_summary()
            return
        for w in self.roms.warnings:
            log.write(f"avviso: {w}")
        wanted = set(self.saved["systems"])
        sl = self.query_one("#systems", SelectionList)
        for s in self.roms.systems.values():
            if not s.urls:
                continue
            sl.add_option(Selection(f"{s.name}  ({len(s.urls)} file)", s.key,
                                    not wanted or s.key in wanted))
        if sl.option_count == 0:
            log.write("roms.yaml non contiene nessun link: compila le liste 'urls'.")
            self.query_one("#start", Button).disabled = True
        self.refresh_summary()
        self.load_sizes()

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
            chosen = [self.roms.systems[k] for k in self._selected_systems()]
            urls = [u for s in chosen for u in s.urls]
            known = sum(self.sizes.get(u) or 0 for u in urls)
            unknown = sum(1 for u in urls if self.sizes.get(u) is None)
            pending = len(self.sizes) == 0
            txt = f"{len(chosen)} sistemi, {len(urls)} file"
            if pending:
                txt += " · calcolo le dimensioni..."
            else:
                txt += f" · ROM scaricate circa {known / 1e9:.1f} GB"
                if unknown:
                    txt += f" (+{unknown} file di dimensione ignota)"
            lines.append(txt)
            if disk:
                usable = pipeline.disk_capacity_bytes(disk) / 1e9
                lines.append(f"Disco {disk:g} GB (utilizzabili circa {usable:.1f} GB): "
                             "l'immagine Batocera da sola occupa circa 8,5 GB.")
        else:
            lines.append("Nessun roms.yaml valido.")
        self.query_one("#summary", Static).update("\n".join(lines))

    @work(thread=True)
    def load_sizes(self) -> None:
        assert self.roms
        urls = [u for s in self.roms.systems.values() for u in s.urls]
        sizes = estimate_sizes(urls)
        self.call_from_thread(self._set_sizes, sizes)

    def _set_sizes(self, sizes: dict[str, int | None]) -> None:
        self.sizes = sizes
        self.refresh_summary()

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
        if event.button.id != "start" or self.running:
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
        config.save(self.cfg_path, {"device": self._device_key(),
                                    "disk_gb": int(disk) if disk == int(disk) else disk,
                                    "systems": keys, "options": opts})
        settings = pipeline.Settings(
            target_key=self._device_key(), disk_gb=disk,
            systems=[self.roms.systems[k] for k in keys],
            verify_md5="verify_md5" in opts, keep_cache="keep_cache" in opts,
            create_all_dirs="create_all_dirs" in opts,
            all_system_keys=tuple(self.roms.systems), root=self.root)
        self.set_running(True)
        self.run_pipeline(settings)

    def set_running(self, value: bool) -> None:
        self.running = value
        self.query_one("#start", Button).disabled = value
        for w in ("#device", "#disk", "#disk_other", "#systems", "#options"):
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

        try:
            final = pipeline.run(settings, log, progress)
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

    def _show_progress(self, label: str, done: int, total: int) -> None:
        self.query_one("#barlabel", Label).update(label)
        bar = self.query_one("#bar", ProgressBar)
        if total > 0:
            bar.update(total=total, progress=done)
        else:
            bar.update(total=None)
