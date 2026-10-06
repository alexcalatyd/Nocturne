"""Cliente mínimo del Web API de Spotify sobre http.client.

Sin dependencias externas, igual que el resto de Nocturne. Reintenta lo que
merece la pena (401 caducado, 429 con Retry-After) y nada más.

Las conexiones se reutilizan (keep-alive). Abrir TCP+TLS contra
api.spotify.com cuesta ~45 ms y `urlopen` lo pagaba en *cada* petición: eso son
~90 ms de más en cada orden de reproducción y en la sincronización del reloj,
que va cada dos segundos.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse

from .auth import AuthError, TokenStore

API = "https://api.spotify.com/v1"
TIMEOUT = 20.0
MAX_RETRY_WAIT = 8.0
POOL_SIZE = 3          # conexiones en reposo por host


class _Pool:
    """Conexiones HTTPS en reposo, listas para la siguiente petición."""

    def __init__(self, size: int = POOL_SIZE) -> None:
        self._size = size
        self._idle: dict[str, list[http.client.HTTPSConnection]] = {}
        self._lock = threading.Lock()

    def take(self, host: str) -> http.client.HTTPSConnection | None:
        with self._lock:
            pool = self._idle.get(host)
            return pool.pop() if pool else None

    def give(self, host: str, conn: http.client.HTTPSConnection) -> None:
        with self._lock:
            pool = self._idle.setdefault(host, [])
            if len(pool) < self._size:
                pool.append(conn)
                return
        _shut(conn)

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, {}
        for pool in idle.values():
            for conn in pool:
                _shut(conn)


def _shut(conn) -> None:
    try:
        conn.close()
    except (OSError, http.client.HTTPException):
        pass


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, tokens: TokenStore | None = None) -> None:
        self.tokens = tokens or TokenStore()
        self._pool = _Pool()

    def close(self) -> None:
        self._pool.close()

    # -- transporte --------------------------------------------------------
    def _send(self, host: str, method: str, target: str, payload: bytes | None,
              headers: dict) -> tuple[int, dict, bytes] | None:
        """Una petición sobre una conexión reutilizada.

        Devuelve None si la conexión que sacamos del pool estaba caducada (el
        servidor la cerró mientras dormía): el llamante reintenta con una nueva.
        """
        conn = self._pool.take(host)
        reused = conn is not None
        if conn is None:
            conn = http.client.HTTPSConnection(host, timeout=TIMEOUT)
        try:
            conn.request(method, target, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (http.client.HTTPException, OSError):
            _shut(conn)
            if reused:
                return None
            raise
        status, head = resp.status, dict(resp.getheaders())
        if resp.will_close or head.get("Connection", "").lower() == "close":
            _shut(conn)
        else:
            self._pool.give(host, conn)
        return status, head, raw

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> dict | None:
        url = path if path.startswith("http") else API + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        payload = json.dumps(body).encode() if body is not None else None

        for attempt in range(3):
            headers = {"Authorization": f"Bearer {self.tokens.access_token()}",
                       "Accept": "application/json"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            try:
                got = self._send(host, method, target, payload, headers)
            except (http.client.HTTPException, OSError) as exc:
                if attempt < 2:
                    time.sleep(0.5)
                    continue
                raise ApiError(f"sin conexión con Spotify: {exc}")
            if got is None:
                continue                      # conexión muerta: otra vuelta
            status, head, raw = got
            if status == 401 and attempt < 2:
                self.tokens.invalidate()
                continue
            if status == 429 and attempt < 2:
                wait = min(MAX_RETRY_WAIT,
                           float(head.get("Retry-After") or 1) + 0.2)
                time.sleep(wait)
                continue
            if status >= 400:
                raise ApiError(_explain(status, raw, path), status)
            if status == 204 or not raw:
                return None
            try:
                return json.loads(raw)
            except ValueError:
                # Los endpoints del reproductor a veces contestan 200 con un
                # cuerpo que no es JSON: no hay nada que leer.
                return None
        raise ApiError("Spotify no respondió")

    def get(self, path: str, **params) -> dict:
        return self.request("GET", path, params=params) or {}

    def put(self, path: str, body: dict | None = None, **params) -> None:
        self.request("PUT", path, params=params, body=body)

    def post(self, path: str, body: dict | None = None, **params) -> None:
        self.request("POST", path, params=params, body=body)

    def pages(self, path: str, limit: int = 50, cap: int = 1000, **params):
        """Itera los elementos de un endpoint paginado."""
        got = 0
        page = self.get(path, limit=limit, **params)
        while page:
            items = page.get("items") or []
            for item in items:
                yield item
                got += 1
                if got >= cap:
                    return
            nxt = page.get("next")
            if not nxt or not items:
                return
            page = self.get(nxt)

    # -- reproducción ------------------------------------------------------
    def devices(self) -> list[dict]:
        return self.get("/me/player/devices").get("devices") or []

    def playback_state(self) -> dict:
        return self.get("/me/player", additional_types="track") or {}

    def play(self, uris: list[str] | None = None, context_uri: str | None = None,
             offset: int | None = None, position_ms: int = 0,
             device_id: str | None = None) -> None:
        body: dict = {"position_ms": max(0, int(position_ms))}
        if context_uri:
            body["context_uri"] = context_uri
            if offset is not None:
                body["offset"] = {"position": offset}
        elif uris:
            body["uris"] = uris
            if offset is not None:
                body["offset"] = {"position": offset}
        self.put("/me/player/play", body=body, device_id=device_id)

    def resume(self, device_id: str | None = None) -> None:
        self.put("/me/player/play", body={}, device_id=device_id)

    def pause(self, device_id: str | None = None) -> None:
        self.put("/me/player/pause", device_id=device_id)

    def skip_next(self, device_id: str | None = None) -> None:
        """Pasa a la siguiente del contexto, la que librespot ya tiene precargada."""
        self.post("/me/player/next", device_id=device_id)

    def seek(self, position_ms: int, device_id: str | None = None) -> None:
        self.put("/me/player/seek", position_ms=max(0, int(position_ms)),
                 device_id=device_id)

    def volume(self, percent: int, device_id: str | None = None) -> None:
        self.put("/me/player/volume", volume_percent=max(0, min(100, int(percent))),
                 device_id=device_id)

    def transfer(self, device_id: str, play: bool = False) -> None:
        self.put("/me/player", body={"device_ids": [device_id], "play": bool(play)})

    def me(self) -> dict:
        return self.get("/me")


def _explain(status: int, raw: bytes, path: str = "") -> str:
    try:
        detail = json.loads(raw or b"{}").get("error", {})
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
    except Exception:
        message = ""
    if status == 403 and "premium" in (message or "").lower():
        return "esta acción requiere Spotify Premium"
    if status == 404 and path.startswith("/me/player"):
        return message or "no hay ningún dispositivo de Spotify activo"
    if status == 404:
        return message or "Spotify no encontró eso"
    return message or f"error {status} de Spotify"


__all__ = ["Client", "ApiError", "AuthError"]
