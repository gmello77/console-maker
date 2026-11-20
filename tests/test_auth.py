import http.server
import json
import stat
import threading

import pytest
import requests
from requests.cookies import get_cookie_header

from cm import auth
from cm.auth import ArchiveAuth, AuthError
from cm.download import DownloadError, download


# ---------------------------------------------------------------- caricamento
def test_load_precedence_env_then_own_file_then_ia_ini(tmp_path):
    assert auth.load_auth(env={}, home=tmp_path) is None
    ini = tmp_path / ".config" / "internetarchive" / "ia.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text("[cookies]\nlogged-in-user = utente%40x.it; domain=.archive.org; path=/\n"
                   "logged-in-sig = firma-ia; domain=.archive.org\n")
    a = auth.load_auth(env={}, home=tmp_path)
    assert (a.user, a.sig) == ("utente%40x.it", "firma-ia")          # attributi del cookie scartati
    auth.save_auth(ArchiveAuth("mio", "mia-sig"), tmp_path)
    assert auth.load_auth(env={}, home=tmp_path).user == "mio"        # il nostro batte ia.ini
    env = {"IA_LOGGED_IN_USER": "da-env", "IA_LOGGED_IN_SIG": "sig-env"}
    assert auth.load_auth(env=env, home=tmp_path).user == "da-env"    # l'ambiente batte tutto
    assert auth.load_auth(env={"IA_LOGGED_IN_USER": "solo-uno"}, home=tmp_path).user == "mio"


def test_corrupt_files_are_ignored(tmp_path):
    p = tmp_path / ".config" / "console-maker" / "archive_auth.json"
    p.parent.mkdir(parents=True)
    p.write_text("{non json")
    assert auth.load_auth(env={}, home=tmp_path) is None
    p.write_text(json.dumps(["lista"]))
    assert auth.load_auth(env={}, home=tmp_path) is None


def test_saved_file_is_private_and_clearable(tmp_path):
    path = auth.save_auth(ArchiveAuth("u", "s"), tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "password" not in path.read_text().lower()
    assert auth.clear_auth(tmp_path) is True and auth.clear_auth(tmp_path) is False
    assert auth.load_auth(env={}, home=tmp_path) is None


def test_secrets_do_not_leak_in_repr():
    assert "segretissimo" not in repr(ArchiveAuth("utente", "segretissimo"))
    assert "segretissimo" not in str(ArchiveAuth("utente", "segretissimo"))


# ---------------------------------------------------------------- cookie
def _header(session, url):
    return get_cookie_header(session.cookies, requests.Request("GET", url).prepare())


def test_cookies_go_to_archive_org_and_its_data_servers_only():
    s = auth.apply(requests.Session(), ArchiveAuth("u", "sig"))
    assert "logged-in-sig=sig" in _header(s, "https://archive.org/download/x/y.zip")
    # il server dei dati a cui archive.org reindirizza i download
    assert "logged-in-user=u" in _header(s, "https://dn720004.ca.archive.org/0/items/x/y.zip")
    assert _header(s, "https://example.com/x") is None                # altri siti: mai
    assert _header(s, "https://evilarchive.org/x") is None            # nemmeno domini simili
    assert _header(s, "http://archive.org/x") is None                 # niente HTTP in chiaro
    assert auth.apply(requests.Session(), None).cookies.get_dict() == {}


def test_download_does_not_send_cookies_to_other_hosts(tmp_path):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self): self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
        def do_GET(self):
            seen.append(self.headers.get("Cookie"))
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        download(f"http://127.0.0.1:{srv.server_port}/x.bin", tmp_path / "x.bin",
                 auth=ArchiveAuth("utente", "firma-segreta"))
    finally:
        srv.shutdown()
    assert (tmp_path / "x.bin").read_bytes() == b"ok"
    assert seen == [None]                                             # nessun cookie uscito


def test_401_with_login_gives_a_different_message(tmp_path):
    class H(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self): self.send_response(401); self.end_headers()
        def do_GET(self): self.send_response(401); self.end_headers()
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/x.zip"
    try:
        with pytest.raises(DownloadError, match="anche con il tuo login"):
            download(url, tmp_path / "a.zip", auth=ArchiveAuth("u", "s"))
        with pytest.raises(DownloadError, match="richiede il login"):
            download(url, tmp_path / "b.zip")
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- login
class FakeResp:
    def __init__(self, status, data): self.status_code, self._d = status, data
    def json(self):
        if isinstance(self._d, Exception): raise self._d
        return self._d


class FakeSession:
    def __init__(self, resp): self.resp, self.headers, self.sent = resp, {}, None
    def post(self, url, params, data, timeout):
        self.sent = (url, params, data)
        if isinstance(self.resp, Exception): raise self.resp
        return self.resp


OK = {"success": True, "values": {"cookies": {"logged-in-user": "u", "logged-in-sig": "s"}}}


def test_login_success_sends_credentials_only_to_the_https_endpoint():
    fs = FakeSession(FakeResp(200, OK))
    a = auth.login(" me@x.it ", "pw-di-prova", fs)
    assert (a.user, a.sig) == ("u", "s")
    url, params, data = fs.sent
    assert url.startswith("https://archive.org/") and params == {"op": "login"}
    assert data == {"email": "me@x.it", "password": "pw-di-prova"}


@pytest.mark.parametrize("resp, text", [
    (FakeResp(401, {"success": False, "values": {"reason": "account_not_found"}}),
     "account non trovato"),
    (FakeResp(200, {"success": False, "values": {"reason": "account_bad_password"}}),
     "password errata"),
    # un 401 generico NON va mai presentato come "password sbagliata"
    (FakeResp(401, {"error": "Unauthorized", "success": False}), "Unauthorized.*HTTP 401"),
    (FakeResp(200, {"success": False, "values": {"reason": "ratelimit"}}), "ratelimit"),
    (FakeResp(502, ValueError("html")), "risposta inattesa"),
    (FakeResp(200, {"success": True, "values": {}}), "non ha restituito la sessione"),
    (requests.ConnectionError("rete giu'"), "impossibile contattare"),
])
def test_login_errors_are_clear_and_never_contain_the_password(resp, text):
    with pytest.raises(AuthError, match=text) as e:
        auth.login("me@x.it", "pw-segretissima", FakeSession(resp))
    assert "pw-segretissima" not in str(e.value)
    if "Unauthorized" in text:
        assert "password errata" not in str(e.value)


def test_login_requires_both_fields():
    for email, pw in (("", "x"), ("a@b.it", ""), ("  ", "x")):
        with pytest.raises(AuthError):
            auth.login(email, pw, FakeSession(FakeResp(200, OK)))
