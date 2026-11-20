"""Login di archive.org per scaricare gli item ad accesso riservato.

Cosa si salva: SOLO i due cookie di sessione (`logged-in-user`, `logged-in-sig`) che
archive.org restituisce al login, mai la password. Stanno in
~/.config/console-maker/archive_auth.json (permessi 600), fuori dalla repo.

Fonti, in ordine: variabili d'ambiente IA_LOGGED_IN_USER / IA_LOGGED_IN_SIG, file nostro,
configurazione dello strumento `ia` se gia' presente.

I cookie vengono legati al dominio .archive.org e al solo HTTPS: requests li rimanda anche
al server dei dati (dnNNN.ca.archive.org, a cui archive.org reindirizza i download) ma mai
ad altri siti.
"""
from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.cookies import create_cookie

from . import USER_AGENT

LOGIN_URL = "https://archive.org/services/xauthn/"
COOKIE_DOMAIN = ".archive.org"


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class ArchiveAuth:
    user: str
    sig: str

    def __repr__(self) -> str:      # niente segreti nei log o nei traceback
        return "ArchiveAuth(<nascosto>)"


def _store_path(home: Path) -> Path:
    return home / ".config" / "console-maker" / "archive_auth.json"


def _ia_ini_paths(home: Path) -> list[Path]:
    return [home / ".config" / "internetarchive" / "ia.ini", home / ".config" / "ia.ini",
            home / ".ia"]


def _cookie_value(raw: str) -> str:
    """ia.ini salva i cookie come 'valore; domain=...; path=/': serve solo il valore."""
    return raw.split(";", 1)[0].strip()


def load_auth(env: dict[str, str] | None = None, home: Path | None = None) -> ArchiveAuth | None:
    env = os.environ if env is None else env
    home = home or Path.home()
    if env.get("IA_LOGGED_IN_USER") and env.get("IA_LOGGED_IN_SIG"):
        return ArchiveAuth(env["IA_LOGGED_IN_USER"], env["IA_LOGGED_IN_SIG"])
    try:
        data = json.loads(_store_path(home).read_text(encoding="utf-8"))
        if data.get("user") and data.get("sig"):
            return ArchiveAuth(data["user"], data["sig"])
    except (FileNotFoundError, ValueError, AttributeError):
        pass
    for p in _ia_ini_paths(home):
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(p, encoding="utf-8")
            user = _cookie_value(cp.get("cookies", "logged-in-user"))
            sig = _cookie_value(cp.get("cookies", "logged-in-sig"))
        except (configparser.Error, OSError):
            continue
        if user and sig:
            return ArchiveAuth(user, sig)
    return None


def save_auth(auth: ArchiveAuth, home: Path | None = None) -> Path:
    path = _store_path(home or Path.home())
    path.parent.mkdir(parents=True, exist_ok=True)
    # il file nasce gia' 600: non esiste nemmeno per un istante leggibile da altri
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"user": auth.user, "sig": auth.sig}, f)
    os.chmod(path, 0o600)
    return path


def clear_auth(home: Path | None = None) -> bool:
    try:
        _store_path(home or Path.home()).unlink()
        return True
    except FileNotFoundError:
        return False


def apply(session: requests.Session, auth: ArchiveAuth | None) -> requests.Session:
    if auth:
        for name, value in (("logged-in-user", auth.user), ("logged-in-sig", auth.sig)):
            session.cookies.set_cookie(create_cookie(
                name=name, value=value, domain=COOKIE_DOMAIN, path="/", secure=True))
    return session


def login(email: str, password: str, session: requests.Session | None = None) -> ArchiveAuth:
    """Autentica su archive.org (stesso endpoint e stessa operazione, `op=login`, di `ia configure`).

    La password va solo a questo URL HTTPS. Non compare mai nei messaggi d'errore.
    """
    if not email.strip() or not password:
        raise AuthError("inserisci email e password")
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", USER_AGENT)
    try:
        r = s.post(LOGIN_URL, params={"op": "login"},
                   data={"email": email.strip(), "password": password}, timeout=30)
    except requests.RequestException as e:
        raise AuthError(f"impossibile contattare archive.org: {type(e).__name__}") from None
    try:
        data = r.json()
    except ValueError:
        raise AuthError(f"risposta inattesa da archive.org (HTTP {r.status_code})") from None
    if not data.get("success"):
        reason = str((data.get("values") or {}).get("reason") or data.get("error") or "")
        # Messaggi solo per i motivi che archive.org dichiara davvero: un 401 generico NON
        # significa "password sbagliata" (con l'operazione sbagliata dava sempre 401).
        if reason == "account_not_found":
            raise AuthError("account non trovato: usa l'email dell'account archive.org, "
                            "non il nome utente")
        if reason == "account_bad_password":
            raise AuthError("password errata")
        raise AuthError(f"login rifiutato da archive.org: {reason or 'nessun motivo indicato'} "
                        f"(HTTP {r.status_code})")
    cookies = (data.get("values") or {}).get("cookies") or {}
    user, sig = cookies.get("logged-in-user"), cookies.get("logged-in-sig")
    if not user or not sig:
        raise AuthError("archive.org ha accettato il login ma non ha restituito la sessione: "
                        "usa `ia configure` e riavvia console-maker")
    return ArchiveAuth(user, sig)
