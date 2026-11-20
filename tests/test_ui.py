import pytest
from textual.widgets import Button, Input, SelectionList

from cm import archive
from cm.archive import ArchiveFile as F
from cm.picker import GamePicker
from cm.ui import ConsoleMakerApp

ROMS = """
systems:
  nes:
    name: Nintendo NES
    urls: [https://archive.org/download/a/b.zip]
  psx:
    name: PlayStation
    item: psx-set
    exclude: demo
  gb:
    name: Game Boy
    urls: []
"""

PSX = [F("Alpha (Disc 1).cue", 1_000_000), F("Alpha (Disc 1).bin", 600_000_000),
       F("Beta.chd", 300_000_000), F("Gamma.chd", 400_000_000), F("Demo.chd", 5)]


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path_factory):
    # i test non devono mai leggere le credenziali vere dell'utente (env o ~/.config)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("IA_LOGGED_IN_USER", raising=False)
    monkeypatch.delenv("IA_LOGGED_IN_SIG", raising=False)
    monkeypatch.setattr("cm.ui.estimate_sizes",
                        lambda urls, **kw: {u: 1_000_000_000 for u in urls})
    monkeypatch.setattr(archive, "list_files", lambda item, cache_dir=None, session=None: PSX)


async def boot(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_menu_lists_only_systems_with_links(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test() as pilot:
        await boot(app, pilot)
        sl = app.query_one("#systems", SelectionList)
        assert sl.option_count == 2                      # gb non ha link
        assert set(sl.selected) == {"nes", "psx"}
        assert len(app.expanded["psx"].urls) == 4        # cue+bin+beta+gamma, demo escluso
        assert app._device_key() == "rg-sp" and app._disk_gb() == 32
        assert not app.query_one("#start", Button).disabled


async def test_choices_change_state(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test() as pilot:
        await boot(app, pilot)
        app.query_one("#systems", SelectionList).deselect_all()
        await pilot.pause()
        assert app._selected_systems() == []
        app.query_one("#disk_other").value = "100"
        await pilot.pause()
        assert app._disk_gb() == 100


async def test_missing_roms_yaml_disables_start(tmp_path):
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test() as pilot:
        await boot(app, pilot)
        assert app.query_one("#start", Button).disabled


async def test_item_error_is_reported_not_hidden(tmp_path, monkeypatch):
    def boom(item, cache_dir=None, session=None):
        raise LookupError("item archive.org non trovato")
    monkeypatch.setattr(archive, "list_files", boom)
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test() as pilot:
        await boot(app, pilot)
        assert app.query_one("#systems", SelectionList).option_count == 1   # solo nes
        log = "\n".join(str(l.text) for l in app.query_one("#log").lines)
        assert "ERRORE psx" in log


async def test_picker_saves_selection_and_reloads(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        sl = app.query_one("#systems", SelectionList)
        sl.highlighted = 1                               # psx
        await pilot.click("#pick")
        await pilot.pause()
        assert isinstance(app.screen, GamePicker)
        picker = app.screen
        assert picker.chosen == {"Alpha", "Beta", "Gamma"}   # parte da "tutti", cue+bin = 1 gioco
        await pilot.click("#none")                           # nessuno dei visibili
        picker.query_one("#q", Input).value = "gam"          # filtra: resta Gamma
        await pilot.pause()
        await pilot.click("#all")
        await pilot.pause()
        assert picker.chosen == {"Gamma"}
        await pilot.click("#ok")
        await boot(app, pilot)
        assert not isinstance(app.screen, GamePicker)
        assert [u.rsplit("/", 1)[1] for u in app.expanded["psx"].urls] == ["Gamma.chd"]
        assert (tmp_path / "selections.yaml").read_text().strip().endswith("- Gamma.chd")
        assert set(sl.selected) == {"nes", "psx"}            # la scelta dei sistemi resta


async def test_picker_cancel_and_non_item_system(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        sl = app.query_one("#systems", SelectionList)
        sl.highlighted = 0                               # nes: niente 'item'
        await pilot.click("#pick")
        await pilot.pause()
        assert not isinstance(app.screen, GamePicker)
        await pilot.pause(0.4)        # un Button ignora un secondo clic durante l'effetto "premuto"
        sl.highlighted = 1
        await pilot.click("#pick")
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
        assert not (tmp_path / "selections.yaml").exists()


async def test_summary_warns_when_selection_does_not_fit(tmp_path, monkeypatch):
    monkeypatch.setattr("cm.ui.estimate_sizes",
                        lambda urls, **kw: {u: 40_000_000_000 for u in urls})     # 40 GB: sul disco da 32 non entra
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        text = str(app.query_one("#summary").render())
        assert "Non ci sta" in text and "verranno tolti" in text        # opzione attiva di default
        from textual.widgets import SelectionList as SL
        app.query_one("#options", SL).deselect("fit_disk")
        await pilot.pause()
        assert "ATTENZIONE: non ci sta" in str(app.query_one("#summary").render())


# ---------------------------------------------------------------- login archive.org
async def test_login_flow_saves_session_not_password_and_logout(tmp_path, monkeypatch):
    from pathlib import Path
    from textual.widgets import Static
    from cm import auth as auth_mod
    from cm.login_screen import LoginScreen
    calls = []

    def fake_login(email, password, session=None):
        calls.append((email, password))
        return auth_mod.ArchiveAuth("utente-ok", "sig-ok")
    monkeypatch.setattr(auth_mod, "login", fake_login)
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        assert "non collegato" in str(app.query_one("#authstatus", Static).render())
        await pilot.click("#open_login")
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        scr = app.screen
        assert scr.query_one("#password", Input).password is True       # password mascherata
        scr.query_one("#email", Input).value = "me@x.it"
        scr.query_one("#password", Input).value = "pw-di-prova"
        await pilot.click("#login")
        await app.workers.wait_for_complete()
        await pilot.pause(0.3)
        assert calls == [("me@x.it", "pw-di-prova")]
        assert not isinstance(app.screen, LoginScreen)
        assert len(app.screen_stack) == 1                              # nessuna finestra rimasta aperta
        assert "collegato:" in str(app.query_one("#authstatus", Static).render())
        saved = (Path.home() / ".config/console-maker/archive_auth.json").read_text()
        assert "utente-ok" in saved and "pw-di-prova" not in saved
        log = "\n".join(str(l.text) for l in app.query_one("#log").lines)
        assert "pw-di-prova" not in log and "me@x.it" not in log

        await pilot.pause(0.4)                                          # effetto "premuto" del Button
        await pilot.click("#open_login")
        await pilot.pause()
        await pilot.click("#logout")
        await pilot.pause(0.3)
        assert "non collegato" in str(app.query_one("#authstatus", Static).render())
        assert not (Path.home() / ".config/console-maker/archive_auth.json").exists()


async def test_login_failure_keeps_dialog_open_with_clear_message(tmp_path, monkeypatch):
    from cm import auth as auth_mod
    from cm.login_screen import LoginScreen
    from textual.widgets import Label

    def bad_login(email, password, session=None):
        raise auth_mod.AuthError("password errata")
    monkeypatch.setattr(auth_mod, "login", bad_login)
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        await pilot.click("#open_login")
        await pilot.pause()
        scr = app.screen
        scr.query_one("#email", Input).value = "me@x.it"
        scr.query_one("#password", Input).value = "sbagliata"
        await pilot.click("#login")
        await app.workers.wait_for_complete()
        await pilot.pause(0.3)
        assert isinstance(app.screen, LoginScreen)
        assert "password errata" in str(scr.query_one("#msg", Label).render())
        assert scr.query_one("#password", Input).value == ""            # svuotata subito
