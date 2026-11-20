import pytest
from textual.widgets import Button, RadioSet, SelectionList

from cm.ui import ConsoleMakerApp

pytestmark = pytest.mark.asyncio

ROMS = """
systems:
  nes:
    name: Nintendo NES
    urls: [https://archive.org/download/a/b.zip]
  snes:
    name: Super Nintendo
    urls: [https://archive.org/download/a/c.zip]
  gb:
    name: Game Boy
    urls: []
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("cm.ui.estimate_sizes",
                        lambda urls: {u: 1_000_000_000 for u in urls})


async def test_menu_lists_only_systems_with_links(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sl = app.query_one("#systems", SelectionList)
        assert sl.option_count == 2
        assert set(sl.selected) == {"nes", "snes"}
        assert app._device_key() == "rg-sp"
        assert app._disk_gb() == 32


async def test_choices_change_state(tmp_path):
    (tmp_path / "roms.yaml").write_text(ROMS)
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#systems", SelectionList).deselect_all()
        await pilot.pause()
        assert app._selected_systems() == []
        app.query_one("#disk_other").value = "100"
        await pilot.pause()
        assert app._disk_gb() == 100


async def test_missing_roms_yaml_disables_start(tmp_path):
    app = ConsoleMakerApp(root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#start", Button).disabled
