"""Persist and verify the QQ analyst's Decision Infra connection settings."""
from __future__ import annotations

import http.client
import os
from pathlib import Path
import re
import shlex
import tempfile
import urllib.error
import urllib.parse

import decision_infra
import userconfig


DEFAULTS = {"DECISION_INFRA": (decision_infra.DEFAULT_BASE_URL,
                                decision_infra.DEFAULT_MODEL)}
ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z_0-9]*)(\s*=\s*)(.*)$")
ALLOWED = {"DECISION_INFRA_BASE_URL", "DECISION_INFRA_MODEL"}


def read_document(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def write_settings(path: Path, original: str, changes: dict[str, str]) -> str:
    """Change only requested assignments and replace the config file atomically."""
    if read_document(path) != original:
        raise ValueError("配置文件已被其他程序修改，请关闭设置窗口后重新打开。")
    if not changes.keys() <= ALLOWED:
        raise ValueError("不支持的配置项。")
    if any(any(char in value for char in "\r\n\0") for value in changes.values()):
        raise ValueError("配置值不能含换行或空字符。")
    remaining = dict(changes)
    lines = []
    for line in original.splitlines(keepends=True):
        match = ASSIGNMENT.match(line.rstrip("\r\n"))
        if match and match[2] in changes:
            key = match[2]
            _, comment = userconfig.split_env_comment(match[4])
            ending = "\n" if line.endswith("\n") else ""
            line = f"{match[1]}{key}{match[3]}{shlex.quote(changes[key])}"
            line += (" " + comment if comment else "") + ending
            remaining.pop(key, None)
        lines.append(line)
    text = "".join(lines)
    if remaining:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "".join(f"export {key}={shlex.quote(value)}\n"
                        for key, value in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return text


def validate_endpoint(base: str) -> str:
    base = base.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("服务地址需为 http(s) 地址，不包含用户名、密码、查询参数或片段。")
    return base


def test_connection(base: str, model: str) -> None:
    """Use the unsaved form values for one synthetic, exact-route decision."""
    base = validate_endpoint(base)
    model = model.strip()
    if not model:
        raise ValueError("请填写精确模型路由。")
    actual = decision_infra.test_decision(base, model)
    if actual != model:
        raise ValueError(f"网关实际路由为 {actual}，与所选模型不一致。")


def error_message(error: Exception) -> str:
    """Hide remote bodies and URLs, which may include private data."""
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 404:
            return "HTTP 404：Decision Infra 没有注册这个精确模型路由。"
        if error.code == 503:
            return "HTTP 503：Decision Infra 已收到请求，但模型 Provider 不可用。"
        return f"HTTP {error.code}：Decision Infra 拒绝了本次判断请求。"
    if isinstance(error, (TimeoutError, OSError, http.client.HTTPException)):
        return "连接失败或超时，请检查服务地址和网络。"
    return "请求未得到有效结果，请检查地址、模型及服务是否支持该接口。"
