import json

import pytest

from cm import archive, config
from cm.archive import ArchiveFile as F
from cm.roms import RomsConfigError, expand_system, parse_roms

META = {"files": [
    {"name": "Game A.nes", "source": "original", "format": "Unknown", "size": "100", "md5": "a"},
    {"name": "Game A.png", "source": "derivative", "format": "PNG", "size": "5"},
    {"name": "__ia_thumb.jpg", "source": "original", "format": "Item Tile", "size": "5"},
    {"name": "x_meta.xml", "source": "original", "format": "Metadata", "size": "5"},
    {"name": "x_archive.torrent", "source": "metadata", "format": "Archive BitTorrent"},
    {"name": "sub/Game B (Rev 1).nes", "source": "original", "size": None},
]}


def test_parse_files_keeps_only_real_originals():
    files = archive.parse_files(META)
    assert [f.name for f in files] == ["Game A.nes", "sub/Game B (Rev 1).nes"]
    assert files[0] == F("Game A.nes", 100, "a") and files[1].size == 0


def test_file_url_quotes_but_keeps_slashes():
    assert archive.file_url("my-item", "sub/Game (EU) #1.zip") == \
        "https://archive.org/download/my-item/sub/Game%20%28EU%29%20%231.zip"


def test_list_files_errors_and_cache(tmp_path):
    with pytest.raises(ValueError):
        archive.list_files("../etc/passwd")

    class Resp:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    class Sess:
        headers = {}
        calls = 0
        def __init__(self, data): self.data = data
        def get(self, url, timeout):
            Sess.calls += 1
            return Resp(self.data)

    with pytest.raises(LookupError):          # item inesistente: l'API risponde {}
        archive.list_files("nope", session=Sess({}))
    s = Sess(META)
    assert len(archive.list_files("ok", tmp_path, s)) == 2
    assert len(archive.list_files("ok", tmp_path, s)) == 2
    assert Sess.calls == 2                    # la seconda lettura viene dalla cache
    assert json.loads((tmp_path / "ok.json").read_text())["files"]


def test_select_files_precedence():
    fs = [F("a.cue", 1), F("a.bin", 2), F("b.chd", 3), F("demo.chd", 4)]
    assert [f.name for f in archive.select_files(fs, include=r"\.chd$", exclude="demo")] == ["b.chd"]
    assert [f.name for f in archive.select_files(fs, names=["a.bin"], include="chd")] == ["a.bin"]
    assert archive.select_files(fs) == fs


def test_group_games_multidisc_and_cue_bin():
    fs = [F("Final Fantasy VII (USA) (Disc 1).cue", 1), F("Final Fantasy VII (USA) (Disc 1).bin", 10),
          F("Final Fantasy VII (USA) (Disc 2).bin", 10), F("Final Fantasy VII (USA).m3u", 1),
          F("Crash (USA).chd", 5)]
    games = archive.group_games(fs)
    assert [g.title for g in games] == ["Crash (USA)", "Final Fantasy VII (USA)"]
    assert len(games[1].files) == 4 and games[1].size == 22


def test_parse_item_and_validation():
    cfg = parse_roms({"systems": {"psx": {"item": "psx-set", "include": r"\.chd$",
                                           "files": ["a.chd"]}}})
    s = cfg.systems["psx"]
    assert s.item == "psx-set" and s.files == ("a.chd",) and s.has_links and s.urls == ()
    for bad in ({"item": "../x"}, {"item": 5}, {"item": "ok", "include": "("},
                {"item": "ok", "files": "a"}):
        with pytest.raises(RomsConfigError):
            parse_roms({"systems": {"psx": bad}})
    warn = parse_roms({"systems": {"psx": {"urls": [], "include": "x"}}}).warnings
    assert any("valgono solo con 'item'" in w for w in warn)


def test_expand_system_precedence_and_warnings():
    allf = [F("a.chd", 10), F("b.chd", 20), F("c.iso", 30)]
    base = parse_roms({"systems": {"psx": {
        "item": "i", "include": r"\.chd$",
        "urls": ["https://archive.org/download/z/x.zip"]}}}).systems["psx"]
    s, w = expand_system(base, allf)
    assert w == [] and len(s.urls) == 3 and s.urls[0].endswith("/z/x.zip")
    assert s.sizes[archive.file_url("i", "b.chd")] == 20
    s, w = expand_system(base, allf, selection=["c.iso", "gone.chd"])   # batte 'include'
    assert [u.rsplit("/", 1)[1] for u in s.urls] == ["x.zip", "c.iso"]
    assert w and "gone.chd" in w[0]
    plain = parse_roms({"systems": {"nes": {"urls": []}}}).systems["nes"]
    assert expand_system(plain, allf) == (plain, [])


def test_selections_roundtrip(tmp_path):
    p = tmp_path / "selections.yaml"
    assert config.load_selections(p) == {}
    config.save_selections(p, {"psx": ["Gioco è.chd"]})
    assert config.load_selections(p) == {"psx": ["Gioco è.chd"]}
    p.write_text("- non un dizionario")
    assert config.load_selections(p) == {}


# ---------------------------------------------------------------- fit
from cm.fit import fit_systems
from cm.roms import System


def _sys(key, names, prio=0):
    return System(key, key, tuple(f"https://archive.org/download/i/{n}" for n in names),
                  priority=prio)


def _sizes(**kw):
    return {f"https://archive.org/download/i/{n.replace('_', ' ')}": v for n, v in kw.items()}


def test_fit_everything_fits_is_unchanged():
    s = _sys("nes", ["a.zip", "b.zip"])
    r = fit_systems([s], {u: 10 for u in s.urls}, budget=100)
    assert not r.changed and r.systems == [s] and r.kept_bytes == 20


def test_fit_priority_beats_order_and_keeps_original_order():
    a, b, c = _sys("a", ["a.zip"]), _sys("b", ["b.zip"], prio=5), _sys("c", ["c.zip"])
    sizes = {u: 60 for s in (a, b, c) for u in s.urls}
    r = fit_systems([a, b, c], sizes, budget=130)       # ci stanno solo due su tre
    assert [s.key for s in r.systems] == ["a", "b"]     # b per priorita', poi a per ordine
    assert r.dropped == {"c": 1} and r.dropped_bytes == 60


def test_fit_keeps_multifile_game_together_and_skips_to_smaller():
    s = _sys("psx", ["Big (Disc 1).bin", "Big (Disc 2).bin", "Small.chd"])
    sizes = {s.urls[0]: 50, s.urls[1]: 50, s.urls[2]: 30}
    r = fit_systems([s], sizes, budget=60)              # "Big" (100) non entra, "Small" si'
    assert [u.rsplit("/", 1)[1] for u in r.systems[0].urls] == ["Small.chd"]
    assert r.dropped == {"psx": 1}
    r = fit_systems([s], sizes, budget=120)             # entra "Big" con entrambi i dischi
    assert [u.rsplit("/", 1)[1] for u in r.systems[0].urls] == \
        ["Big (Disc 1).bin", "Big (Disc 2).bin"]
    assert r.dropped == {"psx": 1}


def test_fit_nothing_fits_and_unknown_sizes():
    s = _sys("nes", ["a.zip", "b.zip"])
    r = fit_systems([s], {s.urls[0]: 500, s.urls[1]: None}, budget=10)
    assert r.dropped == {"nes": 1}                      # a tolto; b (ignota = 0) resta
    assert r.unknown_size_files == 1 and len(r.systems[0].urls) == 1
    assert fit_systems([_sys("x", ["x.zip"])], {"https://archive.org/download/i/x.zip": 5},
                       budget=0).systems == []


def test_priority_parsing():
    cfg = parse_roms({"systems": {"nes": {"priority": 3, "urls": []}}})
    assert cfg.systems["nes"].priority == 3
    for bad in ("alta", True, 1.5):
        with pytest.raises(RomsConfigError):
            parse_roms({"systems": {"nes": {"priority": bad}}})


# ---------------------------------------------------------------- accesso riservato
def test_private_files_are_flagged_and_warned():
    files = archive.parse_files({"files": [
        {"name": "a.zip", "source": "original", "size": "5", "private": "true"},
        {"name": "b.zip", "source": "original", "size": "5"}]})
    assert [f.private for f in files] == [True, False]
    base = parse_roms({"systems": {"nes": {"item": "locked-item"}}}).systems["nes"]
    _, warns = expand_system(base, files)
    assert len(warns) == 1 and "accesso riservato" in warns[0] and "1 file" in warns[0]
    _, warns = expand_system(base, files, selection=["b.zip"])   # solo il pubblico: nessun avviso
    assert warns == []


def test_download_fails_fast_on_401_without_retrying(tmp_path):
    import http.server, threading
    from cm.download import DownloadError, download
    gets = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self): self.send_response(401); self.end_headers()
        def do_GET(self): gets.append(1); self.send_response(401); self.end_headers()
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(DownloadError, match="login di archive.org"):
            download(f"http://127.0.0.1:{srv.server_port}/x.zip", tmp_path / "x.zip")
    finally:
        srv.shutdown()
    assert len(gets) == 1          # un solo tentativo, non quattro con le attese
    assert not (tmp_path / "x.zip").exists()


# ---------------------------------------------------------------- md5 e URL di download
@pytest.mark.parametrize("url, expected", [
    ("https://archive.org/download/my-item/Game.zip", ("my-item", "Game.zip")),
    ("https://archive.org/download/my-item/Game%20%28EU%29.zip", ("my-item", "Game (EU).zip")),
    ("https://archive.org/download/my-item/sub/dir/Game.zip", ("my-item", "sub/dir/Game.zip")),
    ("https://www.archive.org/download/my-item/a.zip", ("my-item", "a.zip")),
    ("https://archive.org/details/my-item", None),                       # non e' un download
    ("https://archive.org/download/my-item", None),                       # manca il file
    ("https://archive.org/download/my-item/", None),
    ("https://example.com/download/my-item/a.zip", None),                 # non e' archive.org
    ("https://evilarchive.org/download/my-item/a.zip", None),
    ("https://ia800000.us.archive.org/0/items/my-item/a.zip", None),      # server dei dati: non riconosciuto
    ("https://archive.org/download/..%2Fetc/a.zip", None),                # item non valido
])
def test_parse_download_url(url, expected):
    assert archive.parse_download_url(url) == expected


def test_parse_download_url_roundtrips_file_url():
    for name in ("Game (EU) #1.zip", "sub/dir/Gioco è.chd", "a b.bin"):
        assert archive.parse_download_url(archive.file_url("itm", name)) == ("itm", name)


def test_expand_system_collects_md5s_lowercased():
    allf = [F("a.chd", 10, "ABCDEF0123456789ABCDEF0123456789"), F("b.chd", 20, None)]
    base = parse_roms({"systems": {"psx": {"item": "i"}}}).systems["psx"]
    s, _ = expand_system(base, allf)
    assert s.md5s == {archive.file_url("i", "a.chd"): "abcdef0123456789abcdef0123456789"}
    assert len(s.urls) == 2                                              # b.chd c'e' ma senza md5
