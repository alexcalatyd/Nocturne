"""Autenticación con Spotify: Authorization Code + PKCE, sin client secret.

El navegador vuelve a un servidor efímero en 127.0.0.1 (Spotify ya no acepta
«localhost» como redirect URI). El refresh token se guarda en
~/.config/nocturne/spotify.json con permisos 600 y se renueva solo.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT_PORT = 8898
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-follow-read",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
)

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "nocturne")
CONFIG_PATH = os.path.join(CONFIG_DIR, "spotify.json")

_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>Nocturne</title>
<body style="font:16px system-ui;background:#111;color:#eee;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<p>&#9834; Nocturne ya tiene acceso. Puedes cerrar esta pestana.</p>"""


class AuthError(Exception):
    pass


def _read_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_config(data: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def stored_client_id() -> str:
    return os.environ.get("NOCTURNE_SPOTIFY_CLIENT_ID") or _read_config().get("client_id", "")


def save_client_id(client_id: str) -> None:
    data = _read_config()
    data["client_id"] = client_id.strip()
    _write_config(data)


def has_credentials() -> bool:
    data = _read_config()
    return bool(stored_client_id() and data.get("refresh_token"))


def forget() -> None:
    data = _read_config()
    data.pop("refresh_token", None)
    _write_config(data)


def _post(payload: dict) -> dict:
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read()).get("error_description", "")
        except Exception:
            pass
        raise AuthError(f"Spotify rechazó la petición ({exc.code}) {detail}".strip())
    except OSError as exc:
        raise AuthError(f"sin conexión con Spotify: {exc}")


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Recoge el ?code= del redirect y se calla."""

    result: dict = {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *args) -> None:      # nada en la terminal
        pass


def authorize(client_id: str, timeout: float = 180.0, open_browser: bool = True) -> str:
    """Flujo interactivo completo. Devuelve el refresh token y lo guarda."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": " ".join(SCOPES),
    })
    url = f"{AUTH_URL}?{params}"

    _Catcher.result = {}
    try:
        server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _Catcher)
    except OSError as exc:
        raise AuthError(f"no se pudo abrir el puerto {REDIRECT_PORT}: {exc}")
    server.timeout = 1.0
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                              daemon=True)
    thread.start()
    if open_browser:
        webbrowser.open(url)

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not _Catcher.result:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()

    result = _Catcher.result
    if not result:
        raise AuthError("se agotó la espera de la autorización")
    if result.get("error"):
        raise AuthError(f"autorización denegada: {result['error']}")
    if result.get("state") != state:
        raise AuthError("el «state» no coincide; posible respuesta ajena")

    tokens = _post({
        "grant_type": "authorization_code",
        "code": result.get("code", ""),
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise AuthError("Spotify no devolvió refresh token")
    data = _read_config()
    data["client_id"] = client_id
    data["refresh_token"] = refresh
    _write_config(data)
    return refresh


class TokenStore:
    """Access token siempre fresco; renueva 60 s antes de caducar."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._access = ""
        self._expires = 0.0

    def access_token(self) -> str:
        with self._lock:
            if self._access and time.monotonic() < self._expires:
                return self._access
            client_id = stored_client_id()
            refresh = _read_config().get("refresh_token", "")
            if not client_id or not refresh:
                raise AuthError("Nocturne no está conectado a Spotify")
            tokens = _post({
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            })
            access = tokens.get("access_token")
            if not access:
                raise AuthError("Spotify no devolvió access token")
            # Spotify rota el refresh token de vez en cuando.
            if tokens.get("refresh_token") and tokens["refresh_token"] != refresh:
                data = _read_config()
                data["refresh_token"] = tokens["refresh_token"]
                _write_config(data)
            self._access = access
            self._expires = time.monotonic() + max(30.0, float(tokens.get("expires_in", 3600)) - 60.0)
            return access

    def invalidate(self) -> None:
        with self._lock:
            self._access = ""
            self._expires = 0.0
