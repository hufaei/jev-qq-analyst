"""Jev-compatible client and settings for the Windows build.

Android parity: two endpoint presets — the official Jev service (user-held
API key, sent only to the configured URL as a bearer token) or the local
Decision Infra gateway (loopback by default, no key; the gateway owns
credentials).  The key is encrypted with DPAPI (Windows' per-user credential
protection, the Keystore equivalent) before touching disk.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import urllib.parse
from ctypes import wintypes
from pathlib import Path

import decision_infra
from decision_infra import DecisionInfraJudge, _json_request, systemone_url

SETTINGS_DIR = Path(os.environ.get("APPDATA", Path.home())) / "jev-qq-analyst"
SETTINGS_PATH = SETTINGS_DIR / "config.json"

JEV_URL = "https://api.typesafe.ai/v1/systemone"
GATEWAY_URL = "http://127.0.0.1:8080/v1/systemone"
DEFAULT_MODEL = "jev-latest"
MODES = {"jev": "官方 Jev", "gateway": "Infra 网关"}
DEFAULT_URLS = {"jev": JEV_URL, "gateway": GATEWAY_URL}


def validated_endpoint(mode: str, url: str) -> str:
    if mode not in MODES:
        raise ValueError("未知连接模式。")
    endpoint = systemone_url(url)
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("外部服务必须使用 HTTPS；HTTP 仅允许本机地址。")
    return endpoint


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _raw(blob: _DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def dpapi_protect(data: str) -> str:
    """Encrypt for the current user; only this user on this machine can decrypt."""
    out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(_blob(data.encode("utf-8"))), None, None, None, None, 0,
            ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    return base64.b64encode(_raw(out)).decode("ascii")


def dpapi_unprotect(token: str) -> str:
    out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(_blob(base64.b64decode(token))), None, None, None, None, 0,
            ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed")
    return _raw(out).decode("utf-8")


def load_settings() -> dict:
    """Return {"mode", "url", "model", "api_key"}; missing entries fall back."""
    settings = {"mode": "jev", "url": JEV_URL, "model": DEFAULT_MODEL, "api_key": ""}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    mode = str(data.get("mode") or "jev")
    settings["mode"] = mode if mode in MODES else "jev"
    settings["url"] = str(data.get("url") or DEFAULT_URLS[settings["mode"]])
    settings["model"] = str(data.get("model") or DEFAULT_MODEL)
    encrypted = data.get("api_key_enc")
    if encrypted:
        try:
            settings["api_key"] = dpapi_unprotect(encrypted)
        except OSError:
            settings["api_key"] = ""
    return settings


def save_settings(mode: str, url: str, model: str, api_key: str) -> None:
    validated_endpoint(mode, url)
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"mode": mode, "url": url.strip(), "model": model.strip()}
    if api_key:
        payload["api_key_enc"] = dpapi_protect(api_key)
    SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def settings_complete(settings: dict | None = None) -> bool:
    settings = settings or load_settings()
    if not (settings["url"].strip() and settings["model"].strip()):
        return False
    try:
        validated_endpoint(settings["mode"], settings["url"])
    except ValueError:
        return False
    return settings["mode"] != "jev" or bool(settings["api_key"].strip())


class JevDirectJudge(DecisionInfraJudge):
    """Same decision contract as the gateway judge, with Android's two modes."""

    name = "jev-direct"

    def __init__(self, settings: dict | None = None, timeout: float = 15.0):
        settings = settings or load_settings()
        if not settings_complete(settings):
            raise ValueError("请先在设置中选择连接模式并填写 URL、模型路由（官方模式还需 API Key）。")
        super().__init__(base_url=settings["url"], model=settings["model"],
                         timeout=timeout)
        self.mode = settings["mode"]
        self.api_key = settings["api_key"]
        self.endpoint = validated_endpoint(self.mode, self.base_url)

    def _post(self, payload: dict) -> dict:
        headers = ({"authorization": f"Bearer {self.api_key}"}
                   if self.mode == "jev" and self.api_key.strip() else None)
        return _json_request("POST", self.endpoint, payload, self.timeout, headers=headers)

    def judge(self, message: str, context: str | None = None,
              quoted_text: str = "") -> dict:
        verdict = super().judge(message, context=context, quoted_text=quoted_text)
        actual_model = str(verdict.get("backend", "")).removeprefix("infra/")
        if self.mode == "gateway" and actual_model != self.model:
            raise ValueError("网关返回了不同的模型路由，请检查模型设置。")
        verdict["backend"] = f"{self.mode}/{actual_model}"
        return verdict


def test_connection(mode: str, url: str, model: str, api_key: str,
                    timeout: float = 15.0) -> str:
    """One synthetic decision; returns the actual route the endpoint used."""
    judge = JevDirectJudge({"mode": mode, "url": url, "model": model, "api_key": api_key},
                           timeout=timeout)
    return judge.judge("连接测试：请判断这是一条普通问候。")["backend"]


if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    current = load_settings()
    print({"configured": settings_complete(current),
           "settings": {k: (v if k != "api_key" else "***") for k, v in current.items()},
           "settings_path": str(SETTINGS_PATH)})
