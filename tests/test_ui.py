import shutil

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


# ---------------------------------------------------------------- BIOS e barre di avanzamento
ROMS_BIOS = ROMS + """
bios:
  psx_bios:
    name: PS1 BIOS
    for: [psx]
    urls: [https://archive.org/download/b/scph1001.bin]
  generic:
    urls: [https://archive.org/download/b/extra.bin]
"""


async def test_summary_counts_bios_and_skip_option(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS_BIOS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        assert {b.key for b in app.selected_bios()} == {"psx_bios", "generic"}
        assert "+ 2 file BIOS" in str(app.query_one("#summary").render())
        # senza il sistema collegato resta solo il BIOS senza 'for'
        sl = app.query_one("#systems", SelectionList)
        sl.deselect("psx")
        await pilot.pause()
        assert [b.key for b in app.selected_bios()] == ["generic"]
        assert "+ 1 file BIOS" in str(app.query_one("#summary").render())
        app.query_one("#options", SelectionList).select("skip_bios")
        await pilot.pause()
        assert app.selected_bios() == []
        assert "BIOS" not in str(app.query_one("#summary").render())


async def test_two_progress_bars_show_file_speed_and_total(tmp_path, monkeypatch):
    from textual.widgets import Label, ProgressBar
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        app._show_progress("ROM nes: Game.zip", 0, 100_000_000, now=0.0)
        app._show_progress("ROM nes: Game.zip", 20_000_000, 100_000_000, now=1.0)
        txt = str(app.query_one("#barlabel", Label).render())
        assert "ROM nes: Game.zip" in txt and "20.0 MB / 100.0 MB" in txt
        assert "20.0 MB/s" in txt and "ancora 4s" in txt
        assert app.query_one("#bar", ProgressBar).progress == 20_000_000
        app._show_overall(1, 4, 0, 400_000_000, now=0.0)
        app._show_overall(2, 4, 100_000_000, 400_000_000, now=1.0)
        txt = str(app.query_one("#overalllabel", Label).render())
        assert "2/4 file" in txt and "100.0 MB / 400.0 MB" in txt and "ancora" in txt
        bar = app.query_one("#overall", ProgressBar)
        assert (bar.progress, bar.total) == (100_000_000, 400_000_000)
        app._show_overall(4, 4, 450_000_000, 400_000_000, now=2.0)   # stima sbagliata: non oltre il 100%
        assert bar.progress == 400_000_000
        app._show_overall(1, 3, 0, 0, now=3.0)                       # dimensioni ignote: si contano i file
        assert (bar.progress, bar.total) == (1, 3)
        app.reset_progress()
        assert str(app.query_one("#overalllabel", Label).render()) == ""
        assert app.query_one("#bar", ProgressBar).progress == 0


# ---------------------------------------------------------------- layout: opzioni sempre raggiungibili
MANY = "systems:\n" + "".join(
    f"  s{i:02d}:\n    name: Sistema {i}\n    urls: [https://archive.org/download/a/f{i}.zip]\n"
    for i in range(14))


@pytest.mark.parametrize("size", [(125, 30), (100, 24), (140, 50)])
async def test_every_option_stays_reachable_after_the_systems_list_fills(tmp_path, size):
    """Bug segnalato: caricata la lista dei sistemi, il box Opzioni scendeva e l'ultima
    opzione non era piu' cliccabile."""
    (tmp_path / "roms.yaml").write_text(MANY)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=size) as pilot:
        await boot(app, pilot)
        opts = app.query_one("#options", SelectionList)
        assert app.query_one("#systems", SelectionList).option_count == 14
        col = opts.parent
        col.scroll_end(animate=False)
        await pilot.pause()
        menu = app.query_one("#menu")
        for i in range(opts.option_count):
            row = opts.content_region.y + i             # riga dell'opzione i (bordo escluso)
            assert menu.region.y <= row < menu.region.bottom, \
                f"opzione {i} fuori dal riquadro del menu a {size}: riga {row}, menu {menu.region}"
        # e il clic sull'ultima funziona davvero
        last = opts.get_option_at_index(opts.option_count - 1).value
        assert last not in opts.selected
        await pilot.click(offset=(opts.content_region.x + 3,
                                  opts.content_region.y + opts.option_count - 1))
        await pilot.pause()
        assert last in opts.selected


# ---------------------------------------------------------------- device.yaml nella schermata
DEVICE_YAML = """
language: it_IT
timezone: Europe/Rome
wifi:
  networks:
    - {ssid: CasaMia, key: password-molto-segreta}
screenscraper: {user: mario, password: scraper-segreta}
"""


async def test_device_yaml_shown_without_secrets_and_skippable(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    (tmp_path / "device.yaml").write_text(DEVICE_YAML)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        text = str(app.query_one("#summary").render())
        assert "Preconfigurazione (device.yaml): lingua it_IT" in text
        assert "1 rete/i Wi-Fi" in text and "ScreenScraper" in text
        for secret in ("password-molto-segreta", "scraper-segreta", "mario", "CasaMia"):
            assert secret not in text                                  # niente segreti a schermo
        assert app.selected_device() is not None
        app.query_one("#options", SelectionList).select("skip_device")
        await pilot.pause()
        assert app.selected_device() is None
        assert "Preconfigurazione" not in str(app.query_one("#summary").render())


async def test_no_device_yaml_means_no_line_and_no_device(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        assert app.selected_device() is None
        assert "Preconfigurazione" not in str(app.query_one("#summary").render())
        assert not app.query_one("#start", Button).disabled


async def test_invalid_device_yaml_blocks_start_and_logs_the_reason_not_the_secrets(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    (tmp_path / "device.yaml").write_text(
        "wifi:\n  networks:\n    - {ssid: CasaMia, key: corta}\nscreenscraper: {user: mario, password: scraper-segreta}\n")
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        await boot(app, pilot)
        assert app.query_one("#start", Button).disabled               # non si parte con un file sbagliato
        log = "\n".join(str(l.text) for l in app.query_one("#log").lines)
        assert "ERRORE" in log and "chiave WPA" in log
        assert "scraper-segreta" not in log and "corta" not in log.replace("chiave WPA", "")


# ---------------------------------------------------------------- spazio sul Mac nel riepilogo
async def test_summary_shows_mac_free_space_and_warns_when_short(tmp_path, monkeypatch):
    import collections
    Usage = collections.namedtuple("Usage", "total used free")
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test(size=(140, 50)) as pilot:
        monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**12, 0, 500 * 10**9))
        await boot(app, pilot)
        text = str(app.query_one("#summary").render())
        assert "Spazio sul Mac: liberi 500 GB" in text and "ATTENZIONE: non bastano" not in text
        monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**12, 0, 4 * 10**9))
        app.refresh_summary()
        text = str(app.query_one("#summary").render())
        assert "Spazio sul Mac: liberi 4 GB" in text and "ATTENZIONE: non bastano" in text
