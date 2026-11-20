"""Download a pezzi in parallelo, provato contro un server HTTP locale che implementa Range."""
import http.server
import json
import os
import re
import threading
import time

import pytest

from cm.auth import ArchiveAuth
from cm.download import DownloadError, download

DATA = os.urandom(3 * 1024 * 1024 + 12345)      # non multiplo del pezzo: l'ultimo e' piu' corto
CHUNK = 256 * 1024
KW = dict(chunk_size=CHUNK, parallel_min=1024 * 1024)


class Server:
    """Server di prova: opzioni per ignorare Range, rifiutare pezzi, rallentare."""

    def __init__(self, data=DATA, ranges=True, delay=0.01, fail_start=None, status_for_get=None):
        self.data, self.ranges, self.delay = data, ranges, delay
        self.fail_start, self.status_for_get = fail_start, status_for_get
        self.requests = []          # (metodo, header Range)
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a): pass

            def do_HEAD(self):
                outer.requests.append(("HEAD", None))
                self.send_response(200)
                self.send_header("Content-Length", str(len(outer.data)))
                self.send_header("Accept-Ranges", "bytes" if outer.ranges else "none")
                self.end_headers()

            def do_GET(self):
                rng = self.headers.get("Range")
                outer.requests.append(("GET", rng))
                if outer.status_for_get:
                    self.send_response(outer.status_for_get)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                m = re.match(r"bytes=(\d+)-(\d*)$", rng or "")
                start = int(m.group(1)) if m else 0
                if outer.fail_start is not None and start == outer.fail_start:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with outer.lock:
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                try:
                    if m and outer.ranges:
                        end = int(m.group(2)) if m.group(2) else len(outer.data) - 1
                        body = outer.data[start:end + 1]
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(outer.data)}")
                    else:                              # nessun Range, o server che lo ignora
                        body = outer.data
                        self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    for i in range(0, len(body), 64 * 1024):
                        time.sleep(outer.delay)
                        try:
                            self.wfile.write(body[i:i + 64 * 1024])
                        except (BrokenPipeError, ConnectionResetError):
                            break                       # il client ha chiuso (es. richiesta di prova)
                finally:
                    with outer.lock:
                        outer.active -= 1

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/file.bin"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def server():
    made = []

    def make(**kw):
        sv = Server(**kw)
        made.append(sv)
        return sv
    yield make
    for sv in made:
        sv.close()


def test_parallel_download_is_correct_and_really_concurrent(tmp_path, server):
    sv = server()
    seen = []
    caller = threading.get_ident()
    threads = set()

    def progress(d, t):
        threads.add(threading.get_ident())
        seen.append((d, t))
    out = download(sv.url, tmp_path / "f.bin", progress, connections=4, **KW)
    assert out.read_bytes() == DATA
    assert sv.max_active >= 2                              # piu' connessioni contemporanee
    assert seen[-1] == (len(DATA), len(DATA))
    assert all(a[0] <= b[0] for a, b in zip(seen, seen[1:]))   # mai all'indietro
    assert threads == {caller}                             # callback solo dal thread chiamante
    assert not list(tmp_path.glob("*.part*"))              # niente residui


def test_falls_back_to_simple_download_when_server_ignores_range(tmp_path, server):
    sv = server(ranges=False)
    out = download(sv.url, tmp_path / "f.bin", connections=4, **KW)
    assert out.read_bytes() == DATA
    gets = [r[1] for r in sv.requests if r[0] == "GET"]
    assert gets == ["bytes=0-0", None]                     # prova, poi un unico download intero
    assert not list(tmp_path.glob("*.part*"))


def test_one_connection_or_small_file_uses_simple_download(tmp_path, server):
    sv = server()
    download(sv.url, tmp_path / "a.bin", connections=1, **KW)
    assert sv.max_active == 1
    assert all(r[1] != "bytes=0-0" for r in sv.requests)   # nessuna richiesta di prova
    sv2 = server()
    download(sv2.url, tmp_path / "b.bin", connections=4, chunk_size=CHUNK,
             parallel_min=len(DATA) + 1)                    # file sotto la soglia
    assert all(r[1] != "bytes=0-0" for r in sv2.requests)
    assert (tmp_path / "b.bin").read_bytes() == DATA


def test_resume_downloads_only_missing_chunks(tmp_path, server):
    fail_at = 5 * CHUNK                                    # un pezzo in mezzo non e' scaricabile
    sv = server(fail_start=fail_at)
    with pytest.raises(DownloadError, match="404"):
        download(sv.url, tmp_path / "f.bin", connections=4, **KW)
    state = json.loads((tmp_path / "f.bin.part.state").read_text())
    assert fail_at // CHUNK not in state["done"] and len(state["done"]) >= 1
    assert not (tmp_path / "f.bin").exists()
    done_before = set(state["done"])

    sv.fail_start = None                                   # il server torna a posto
    sv.requests.clear()
    out = download(sv.url, tmp_path / "f.bin", connections=4, **KW)
    assert out.read_bytes() == DATA
    starts = {int(re.match(r"bytes=(\d+)-", r[1]).group(1)) // CHUNK
              for r in sv.requests if r[0] == "GET" and r[1] and r[1] != "bytes=0-0"}
    assert starts.isdisjoint(done_before)                  # i pezzi gia' fatti non si riscaricano
    assert fail_at // CHUNK in starts


def test_changed_chunk_size_restarts_instead_of_corrupting(tmp_path, server):
    sv = server(fail_start=5 * CHUNK)
    with pytest.raises(DownloadError):
        download(sv.url, tmp_path / "f.bin", connections=4, **KW)
    sv.fail_start = None
    out = download(sv.url, tmp_path / "f.bin", connections=4, chunk_size=CHUNK * 2,
                   parallel_min=1024 * 1024)
    assert out.read_bytes() == DATA


def test_corrupt_state_file_is_ignored(tmp_path, server):
    sv = server()
    (tmp_path / "f.bin.part").write_bytes(b"x" * 10)
    (tmp_path / "f.bin.part.state").write_text("{non json")
    assert download(sv.url, tmp_path / "f.bin", connections=4, **KW).read_bytes() == DATA


def test_401_in_parallel_mode_fails_fast_with_clear_message(tmp_path, server):
    sv = server(status_for_get=401)
    with pytest.raises(DownloadError, match="login di archive.org"):
        download(sv.url, tmp_path / "f.bin", connections=4, **KW)
    gets = [r for r in sv.requests if r[0] == "GET"]
    assert len(gets) == 1                                  # nessun tentativo ripetuto
    sv2 = server(status_for_get=401)
    with pytest.raises(DownloadError, match="anche con il tuo login"):
        download(sv2.url, tmp_path / "g.bin", connections=4, auth=ArchiveAuth("u", "s"), **KW)


def test_legacy_sequential_partial_is_completed_sequentially(tmp_path, server):
    sv = server()
    (tmp_path / "f.bin.part").write_bytes(DATA[:1000])     # .part senza .state: vecchio formato
    out = download(sv.url, tmp_path / "f.bin", connections=4, **KW)
    assert out.read_bytes() == DATA
    assert sv.max_active == 1
