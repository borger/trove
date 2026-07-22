"""RomM REST client — stdlib-only (urllib + json).

Ported from the mister-companion prototype with all `requests` calls replaced
by ``urllib.request`` so Trove has zero external Python dependencies. Preserves
the same public API: ``authenticate`` / ``exchange_pairing_code`` / ``whoami`` /
``get_platforms`` / ``get_collections`` / ``get_roms`` / ``stream_rom`` /
``get_saves`` / ``get_states`` / ``stream_asset`` / ``upload_asset`` /
``update_asset`` / ``get_firmware`` / ``stream_firmware``.

Auth flow: exchange a 60-second pairing code from RomM's Control Panel → API
Keys for a long-lived bearer token. Same UX as decky-romm-sync.
"""
from __future__ import annotations

import io
import json
import re
import ssl
import uuid
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT = 15
LIST_TIMEOUT = 60
CHUNK_SIZE = 256 * 1024
_PAIR_CODE_RE = re.compile(r"[^A-Za-z0-9]")


class RomMError(Exception):
    pass


class RomMPairingError(RomMError):
    """Pairing-code exchange rejected. ``reason`` is a short machine tag."""

    def __init__(self, message: str, reason: str = "invalid"):
        super().__init__(message)
        self.reason = reason


def normalize_pairing_code(code: str) -> str:
    """Strip whitespace/dashes, uppercase, alphanumerics only."""
    return _PAIR_CODE_RE.sub("", (code or "")).upper()


class _StreamingResponse:
    """Thin wrapper mimicking ``requests.Response`` streaming shape for callers."""

    def __init__(self, response):
        self._response = response
        self.status_code = response.status
        self.headers = {k.lower(): v for k, v in response.headers.items()}

    def iter_content(self, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        while True:
            chunk = self._response.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:
            pass


def _build_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """Hand-rolled multipart/form-data body — stdlib has no builder for this.

    files: {field_name: (filename, raw_bytes, content_type)}
    Returns (body_bytes, content_type_with_boundary).
    """
    boundary = f"----trove{uuid.uuid4().hex}"
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(f"{value}\r\n".encode())
    for field_name, (filename, data, content_type) in files.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        )
        buf.write(f"Content-Type: {content_type}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


class RomMClient:
    def __init__(self, base_url: str, token: str = "", verify_tls: bool = True):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.verify_tls = verify_tls
        self._token = token or ""
        # A shared SSLContext (verify off if requested) — we can't use requests.Session,
        # so we manage headers ourselves per-call.
        self._ssl_ctx = ssl.create_default_context()
        if not verify_tls:
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ── URL + auth helpers ────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        if not self.base_url:
            raise RomMError("RomM URL is empty")
        return f"{self.base_url}{path}"

    @property
    def token(self) -> str:
        return self._token

    def set_token(self, token: str) -> None:
        self._token = token or ""

    def _headers(self, *, extra: dict | None = None, authed: bool = True) -> dict:
        h = {"Accept": "application/json", "User-Agent": "Trove/0.1"}
        if authed and self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if extra:
            h.update(extra)
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        stream: bool = False,
        authed: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """Low-level HTTP call. Returns dict/list (JSON) or _StreamingResponse."""
        if authed and not self._token:
            raise RomMError("No API token — pair the client first")

        url = self._url(path)
        if params:
            url += "?" + urlencode(params)

        body: bytes | None = None
        headers = self._headers(authed=authed)
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            body = raw_body
            if content_type:
                headers["Content-Type"] = content_type

        req = Request(url, data=body, method=method, headers=headers)
        try:
            resp = urlopen(req, timeout=timeout, context=self._ssl_ctx)
        except HTTPError as e:
            # RomM returns error bodies with detail; surface it.
            try:
                detail = e.read().decode("utf-8", errors="ignore")[:500]
            except Exception:
                detail = ""
            self._raise_for_status(e.code, path, detail)
        except URLError as e:
            raise RomMError(f"Cannot reach RomM ({path}): {e.reason}") from e

        if stream:
            return _StreamingResponse(resp)

        try:
            data = resp.read()
        finally:
            resp.close()
        if not data:
            return {}
        return json.loads(data.decode("utf-8", errors="ignore"))

    def _raise_for_status(self, status: int, path: str, detail: str) -> None:
        if status == 401:
            raise RomMError("Token was revoked or is invalid — pair again")
        if status == 403:
            raise RomMError(f"Access denied ({path}) — token lacks required scope")
        raise RomMError(f"{path} → HTTP {status}: {detail}")

    # ── unauthenticated / setup ───────────────────────────────────────────
    def heartbeat(self) -> dict:
        return self._request("GET", "/api/heartbeat", authed=False)

    def exchange_pairing_code(self, code: str) -> dict:
        """One-shot exchange of a 60s pairing code for a Client API Token.

        Public endpoint — the code is itself the credential; no Authorization
        header is sent. Returns the ``ClientTokenCreateSchema``; its ``raw_token``
        becomes this client's bearer.
        """
        normalized = normalize_pairing_code(code)
        if not normalized:
            raise RomMPairingError("Enter a pairing code", reason="empty")

        try:
            payload = self._request(
                "POST",
                "/api/client-tokens/exchange",
                json_body={"code": normalized},
                authed=False,
            )
        except RomMError as exc:
            msg = str(exc)
            if "HTTP 404" in msg:
                raise RomMPairingError("Pairing code is invalid or expired", reason="invalid")
            if "HTTP 403" in msg:
                raise RomMPairingError("Pairing token owner is disabled", reason="forbidden")
            if "HTTP 429" in msg:
                raise RomMPairingError("Too many pairing attempts — wait a minute", reason="rate_limited")
            raise RomMPairingError(msg, reason="server_error") from exc

        raw = payload.get("raw_token") if isinstance(payload, dict) else None
        if not raw:
            raise RomMPairingError("RomM returned no raw_token", reason="server_error")
        self.set_token(raw)
        return payload

    # ── platforms / collections / roms ────────────────────────────────────
    def whoami(self) -> dict:
        return self._request("GET", "/api/users/me")

    def get_platforms(self) -> list[dict]:
        data = self._request("GET", "/api/platforms")
        return data if isinstance(data, list) else data.get("items", [])

    def get_collections(self) -> list[dict]:
        data = self._request("GET", "/api/collections")
        return data if isinstance(data, list) else data.get("items", [])

    def get_roms(
        self,
        *,
        platform_id: int | None = None,
        collection_id: int | None = None,
        limit: int = 5000,
        offset: int = 0,
        with_files: bool = False,
    ) -> list[dict]:
        params: dict = {
            "limit": limit,
            "offset": offset,
            "order_by": "name",
            "with_files": "true" if with_files else "false",
            "with_char_index": "false",
            "with_filter_values": "false",
        }
        if platform_id is not None:
            params["platform_ids"] = platform_id
        if collection_id is not None:
            params["collection_id"] = collection_id
        data = self._request("GET", "/api/roms", params=params, timeout=LIST_TIMEOUT)
        return data.get("items", data) if isinstance(data, dict) else data

    def stream_rom(self, rom_id: int, file_name: str) -> _StreamingResponse:
        path = f"/api/roms/{rom_id}/content/{quote(file_name, safe='')}"
        return self._request("GET", path, stream=True, timeout=LIST_TIMEOUT)

    # ── saves / states ────────────────────────────────────────────────────
    def get_saves(self, rom_id: int) -> list[dict]:
        data = self._request("GET", "/api/saves", params={"rom_id": rom_id})
        return data if isinstance(data, list) else data.get("items", [])

    def get_states(self, rom_id: int) -> list[dict]:
        data = self._request("GET", "/api/states", params={"rom_id": rom_id})
        return data if isinstance(data, list) else data.get("items", [])

    def stream_asset(self, asset_kind: str, asset_id: int) -> _StreamingResponse:
        if asset_kind not in ("save", "state"):
            raise RomMError(f"unknown asset_kind {asset_kind!r}")
        return self._request(
            "GET", f"/api/{asset_kind}s/{asset_id}/content",
            stream=True, timeout=LIST_TIMEOUT,
        )

    def upload_asset(
        self,
        asset_kind: str,
        rom_id: int,
        file_name: str,
        payload: bytes,
        emulator: str = "mister",
        slot: str | None = None,
    ) -> dict:
        if asset_kind not in ("save", "state"):
            raise RomMError(f"unknown asset_kind {asset_kind!r}")
        params: dict = {"rom_id": rom_id, "emulator": emulator}
        if slot is not None and asset_kind == "save":
            params["slot"] = slot
        field = "saveFile" if asset_kind == "save" else "stateFile"
        body, ctype = _build_multipart(
            fields={},
            files={field: (file_name, payload, "application/octet-stream")},
        )
        return self._request(
            "POST", f"/api/{asset_kind}s",
            params=params, raw_body=body, content_type=ctype,
            timeout=LIST_TIMEOUT,
        )

    def update_asset(
        self,
        asset_kind: str,
        asset_id: int,
        file_name: str,
        payload: bytes,
    ) -> dict:
        if asset_kind not in ("save", "state"):
            raise RomMError(f"unknown asset_kind {asset_kind!r}")
        field = "saveFile" if asset_kind == "save" else "stateFile"
        body, ctype = _build_multipart(
            fields={},
            files={field: (file_name, payload, "application/octet-stream")},
        )
        return self._request(
            "PUT", f"/api/{asset_kind}s/{asset_id}",
            raw_body=body, content_type=ctype,
            timeout=LIST_TIMEOUT,
        )

    # ── firmware / BIOS ───────────────────────────────────────────────────
    def get_firmware(self, platform_id: int) -> list[dict]:
        data = self._request("GET", "/api/firmware", params={"platform_id": platform_id})
        return data if isinstance(data, list) else data.get("items", [])

    def stream_firmware(self, firmware_id: int, file_name: str) -> _StreamingResponse:
        path = f"/api/firmware/{firmware_id}/content/{quote(file_name, safe='')}"
        return self._request("GET", path, stream=True, timeout=LIST_TIMEOUT)
