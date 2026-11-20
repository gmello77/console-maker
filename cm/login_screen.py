"""Finestra di login archive.org. La password non viene mai salvata né scritta nel log."""
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from . import auth


class LoginScreen(ModalScreen["bool | None"]):
    """Chiude con True se il login e' riuscito (o e' stato fatto il logout), None se annullata."""

    BINDINGS = [("escape", "cancel", "Annulla")]
    CSS = """
    LoginScreen { align: center middle; }
    #box { width: 64; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    #box Input { margin-bottom: 1; }
    #msg { height: auto; margin-bottom: 1; }
    #buttons { height: auto; }
    #buttons Button { margin-right: 1; }
    """

    def __init__(self, logged_in: bool) -> None:
        super().__init__()
        self.logged_in = logged_in

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Login archive.org (serve per gli item ad accesso riservato)")
            yield Label("Email / account")
            yield Input(id="email", placeholder="nome@esempio.it")
            yield Label("Password")
            yield Input(id="password", password=True)
            yield Label("La password va solo a archive.org e non viene salvata: "
                        "si conserva la sessione, in ~/.config/console-maker (permessi 600).",
                        id="msg")
            with Horizontal(id="buttons"):
                yield Button("Accedi", id="login", variant="success")
                if self.logged_in:
                    yield Button("Disconnetti", id="logout", variant="warning")
                yield Button("Annulla", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#email", Input).focus()

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self.start_login()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()      # il gestore dell'app non deve vedere i pulsanti di questa finestra
        if event.button.id == "login":
            self.start_login()
        elif event.button.id == "logout":
            auth.clear_auth()
            self.dismiss(True)
        elif event.button.id == "cancel":
            self.dismiss(None)

    def start_login(self) -> None:
        email = self.query_one("#email", Input).value
        password = self.query_one("#password", Input).value
        self.query_one("#password", Input).value = ""      # non resta nel widget
        self.query_one("#login", Button).disabled = True
        self.query_one("#msg", Label).update("Accesso in corso...")
        self.do_login(email, password)

    @work(thread=True, exclusive=True)
    def do_login(self, email: str, password: str) -> None:
        try:
            a = auth.login(email, password)
            auth.save_auth(a)
        except auth.AuthError as e:
            self.app.call_from_thread(self._failed, str(e))
        else:
            self.app.call_from_thread(self.dismiss, True)

    def _failed(self, message: str) -> None:
        self.query_one("#msg", Label).update(f"Errore: {message}")
        self.query_one("#login", Button).disabled = False
        self.query_one("#password", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)
