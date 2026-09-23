"""Load the QQ analyst's environment settings.

The active HUD uses DECISION_INFRA_BASE_URL (default http://127.0.0.1:8080)
and DECISION_INFRA_MODEL (default jev-latest). Provider credentials belong to
the Decision Infra process, not this application.

Settings are shell-style KEY=value. The real environment wins, followed by
user configuration directories and the project .env. The old jev-jarvis
directory name is retained so existing installations keep working.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

PROJECT_ENV = Path(__file__).resolve().parent.parent / ".env"
ACTIVE_KEYS = {"DECISION_INFRA_BASE_URL", "DECISION_INFRA_MODEL"}


def config_dirs() -> list[Path]:
    """Candidate config homes, most specific first.

    macOS puts GUI-app data in ~/Library/Application Support; developer CLI tools
    conventionally use ~/.config (XDG). This app is both, so we read either — and say
    which one won in `--check`, because a silently ignored config file is worse than none.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    dirs = []
    if xdg:
        dirs.append(Path(xdg) / "jev-jarvis")
    dirs.append(Path.home() / ".config" / "jev-jarvis")
    dirs.append(Path.home() / "Library" / "Application Support" / "jev-jarvis")
    return dirs


def env_files() -> list[Path]:
    """The `env` file in each candidate directory, in priority order."""
    return [d / "env" for d in config_dirs()]


# kept for callers that want to name the canonical (dev-tool) location
CONFIG_DIR = Path.home() / ".config" / "jev-jarvis"
ENV_FILE = CONFIG_DIR / "env"


def split_env_comment(value: str) -> tuple[str, str]:
    """Split shell comments outside quotes, including escaped/concatenated quotes."""
    quote = None
    escaped = False
    for i, ch in enumerate(value):
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and i > 0 and value[i - 1] in " \t":
            return value[:i].rstrip(), value[i:]
    return value, ""


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style env file: KEY=VALUE, optional `export`, quotes, # comments.

    Comments are stripped by scanning rather than splitting on " #": splitting first used
    to skip the unquoting step, which turned `KEY=""` into the two characters `""`
    (truthy!) and left literal quotes inside real keys.
    """
    out: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()

        val, _comment = split_env_comment(val)
        try:
            lexer = shlex.shlex(val, posix=True)
            lexer.whitespace = ""
            lexer.commenters = ""
            val = "".join(lexer)
        except ValueError:
            continue  # malformed shell quoting is not a usable setting
        if key:
            out[key] = val
    return out


def _merged_env_file() -> dict[str, str]:
    """Union of every candidate `env` file; earlier directories win on conflicts."""
    out: dict[str, str] = {}
    for f in reversed(env_files()):
        out.update(parse_env_file(f))
    return out


def _label() -> str:
    """Name the source by the real file(s) it read, not by a generic label."""
    paths = [f for f in env_files() if parse_env_file(f)]
    if not paths:
        return "env"
    home = str(Path.home())
    return ", ".join(str(f).replace(home, "~") for f in paths)


_startup_sources: list[tuple[str, dict[str, str]]] | None = None

def _sources() -> list[tuple[str, dict[str, str]]]:
    if _startup_sources is not None:
        return _startup_sources
    return [
        ("环境变量", {key: value for key, value in os.environ.items()
                  if key in ACTIVE_KEYS}),
        (_label(), {key: value for key, value in _merged_env_file().items()
                    if key in ACTIVE_KEYS}),
        (str(PROJECT_ENV), {key: value for key, value in parse_env_file(PROJECT_ENV).items()
                            if key in ACTIVE_KEYS}),
    ]


def get(*names: str) -> str:
    """First non-empty value among `names`, searching sources in priority order."""
    for _src, vals in _sources():
        for name in names:
            if vals.get(name):
                return vals[name]
    return ""


def source_of(*names: str) -> str:
    for src, vals in _sources():
        for name in names:
            if vals.get(name):
                return src
    return "none"


def load() -> dict[str, str]:
    """Load only active gateway settings; legacy provider secrets stay on disk."""
    global _startup_sources
    # Keep this process on its startup configuration: settings saves require restart.
    if _startup_sources is None:
        _startup_sources = _sources()
    loaded = {key: value for key, value in _merged_env_file().items()
              if key in ACTIVE_KEYS}
    for key, val in loaded.items():
        if val and not os.environ.get(key):
            os.environ[key] = val
    return loaded


def where() -> str:
    """Which config file is actually supplying the keys — for --check style output."""
    for f in env_files():
        if parse_env_file(f):
            return str(f)
    return "（未找到配置文件）"
