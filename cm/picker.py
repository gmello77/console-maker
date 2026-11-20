"""Finestra per scegliere i singoli giochi di un item archive.org."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, SelectionList
from textual.widgets.selection_list import Selection

from .archive import Game

MAX_SHOWN = 1000  # oltre, la lista diventa lenta: si restringe con la ricerca


def human(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


class GamePicker(ModalScreen["list[str] | None"]):
    """Restituisce i nomi file scelti, oppure None se annullata."""

    BINDINGS = [("escape", "cancel", "Annulla")]
    CSS = """
    GamePicker { align: center middle; }
    #dlg { width: 90%; height: 90%; border: round $primary; background: $surface; padding: 0 1; }
    #games { height: 1fr; }
    #info { height: auto; }
    #buttons { height: auto; }
    #buttons Button { margin-right: 1; }
    """

    def __init__(self, title: str, games: list[Game], chosen_files: list[str] | None) -> None:
        super().__init__()
        self.heading = title
        self.games = games
        # Senza scelta precedente il comportamento di default e' "tutti": si parte da li'.
        self.chosen: set[str] = {
            g.title for g in games
            if chosen_files is None or any(f.name in set(chosen_files) for f in g.files)}
        self.shown: list[Game] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label(f"{self.heading}: scegli i giochi (spazio = spunta)")
            yield Input(placeholder="cerca per nome...", id="q")
            yield SelectionList(id="games")
            yield Label("", id="info")
            with Horizontal(id="buttons"):
                yield Button("Tutti i visibili", id="all")
                yield Button("Nessuno dei visibili", id="none")
                yield Button("Conferma", id="ok", variant="success")
                yield Button("Annulla", id="cancel")

    def on_mount(self) -> None:
        self.refill()
        self.query_one("#q", Input).focus()

    def refill(self) -> None:
        q = self.query_one("#q", Input).value.strip().lower()
        matching = [g for g in self.games if q in g.title.lower()]
        self.shown = matching[:MAX_SHOWN]
        sl = self.query_one("#games", SelectionList)
        sl.clear_options()
        sl.add_options([Selection(f"{g.title}  ({human(g.size)})", g.title, g.title in self.chosen)
                        for g in self.shown])
        self.refresh_info(len(matching))

    def refresh_info(self, matching: int | None = None) -> None:
        by_title = {g.title: g for g in self.games}
        size = sum(by_title[t].size for t in self.chosen)
        text = f"{len(self.chosen)} giochi scelti su {len(self.games)}, {human(size)}"
        if matching is not None and matching > MAX_SHOWN:
            text += f" · mostrati i primi {MAX_SHOWN} di {matching}: restringi la ricerca"
        self.query_one("#info", Label).update(text)

    def on_input_changed(self, _: Input.Changed) -> None:
        self.refill()

    def on_selection_list_selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
        title = event.selection.value
        if title in self.query_one("#games", SelectionList).selected:
            self.chosen.add(title)
        else:
            self.chosen.discard(title)
        self.refresh_info()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id
        if bid in ("all", "none"):
            titles = {g.title for g in self.shown}
            self.chosen = (self.chosen | titles) if bid == "all" else (self.chosen - titles)
            self.refill()
        elif bid == "ok":
            if not self.chosen:
                self.notify("Scegli almeno un gioco.", severity="warning")
                return
            names = [f.name for g in self.games if g.title in self.chosen for f in g.files]
            self.dismiss(names)
        elif bid == "cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
