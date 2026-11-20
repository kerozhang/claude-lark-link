"""Bridge settings and separate local credentials for the plugin and the app."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex

DEFAULT_DIR = Path.home() / ".claude-lark-link"
DEFAULT_PORT = 8765
DEFAULT_PREFIX = "/api/v1"
PREFIX = re.compile(r"(?:/[A-Za-z0-9_-]+)+")
TOKEN = re.compile(r"[0-9a-f]{64}")


def parse_workspaces(raw: object) -> dict[str, Path]:
    if not isinstance(raw, dict):
        raise ValueError("workspaces must be an object of aliases and absolute paths")
    workspaces = {}
    for alias, value in raw.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", alias):
            raise ValueError("invalid workspace alias")
        if not isinstance(value, str) or value.startswith(("//", "\\\\")):
            raise ValueError("workspace paths must be local directories")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("workspace paths must be absolute")
        path = path.resolve(strict=True)
        if not path.is_dir() or path == path.parent or str(path).startswith(("//", "\\\\")):
            raise ValueError("invalid workspace directory")
        if path in workspaces.values():
            raise ValueError("each workspace directory must have one alias")
        workspaces[alias] = path
    return workspaces


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class BridgeConfig:
    domain: str = "feishu"
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    open_id: str = ""
    workspaces: dict[str, Path] = field(default_factory=dict)
    port: int = DEFAULT_PORT
    api_prefix: str = DEFAULT_PREFIX
    hook_token: str = field(default="", repr=False)
    allow_auto_connect: bool = False
    data_dir: Path = DEFAULT_DIR

    @property
    def open_platform(self) -> str:
        return "https://open.larksuite.com" if self.domain == "lark" else "https://open.feishu.cn"

    @property
    def base_url(self) -> str:
        return self.open_platform + "/open-apis"

    @property
    def enabled(self) -> bool:
        return bool(
            self.domain in {"feishu", "lark"} and re.fullmatch(r"cli_[A-Za-z0-9]+", self.app_id)
            and self.app_secret and re.fullmatch(r"ou_[A-Za-z0-9]+", self.open_id)
            and TOKEN.fullmatch(self.hook_token) and self.workspaces
            and 1 <= self.port <= 65535 and PREFIX.fullmatch(self.api_prefix)
        )


def load_config(directory: Path = DEFAULT_DIR) -> BridgeConfig:
    data = json.loads((directory / "config.json").read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("config.json must contain an object")
    app = read_env(directory / ".env")
    local = read_env(directory / "plugin" / ".env")
    domain = data.get("domain", "feishu")
    port, prefix = data.get("port", DEFAULT_PORT), data.get("api_prefix", DEFAULT_PREFIX)
    auto = data.get("allow_auto_connect", False)
    if (domain not in ("feishu", "lark") or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(prefix, str) or not PREFIX.fullmatch(prefix) or type(auto) is not bool):
        raise ValueError("invalid domain, port, API prefix, or auto-connect setting")
    return BridgeConfig(
        domain=domain, app_id=app.get("LARK_APP_ID", ""), app_secret=app.get("LARK_APP_SECRET", ""),
        open_id=app.get("LARK_OWNER_OPEN_ID", ""), workspaces=parse_workspaces(data.get("workspaces", {})),
        port=port, api_prefix=prefix, hook_token=local.get("CLAUDE_LARK_HOOK_TOKEN", ""),
        allow_auto_connect=auto, data_dir=directory,
    )


def initialize(directory: Path = DEFAULT_DIR) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "plugin").mkdir(mode=0o700, exist_ok=True)
    defaults = {
        directory / "config.json": json.dumps({
            "domain": "feishu", "workspaces": {}, "port": DEFAULT_PORT, "api_prefix": DEFAULT_PREFIX,
            "allow_auto_connect": False,
        }, indent=2) + "\n",
        directory / ".env": "LARK_APP_ID=\nLARK_APP_SECRET=\nLARK_OWNER_OPEN_ID=\n",
        directory / "plugin" / ".env": f"CLAUDE_LARK_HOOK_TOKEN={token_hex(32)}\n",
    }
    for path, text in defaults.items():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
