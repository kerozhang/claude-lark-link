"""Minimal async Feishu / Lark OpenAPI client for interactive card messaging."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import BridgeConfig

@dataclass
class _TokenState:
    token: str = ""
    expires_at: float = 0.0


class LarkIMClient:
    """Async REST client supporting both Feishu (CN) and Lark (Global) OpenAPI."""

    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://open.feishu.cn/open-apis") -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        if base_url not in {"https://open.feishu.cn/open-apis", "https://open.larksuite.com/open-apis"}:
            raise ValueError("Unsupported Feishu / Lark API domain")
        self._base_url = base_url
        self._token = _TokenState()
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None

    @classmethod
    def from_config(cls, config: BridgeConfig) -> "LarkIMClient":
        if not config.app_id or not config.app_secret:
            raise RuntimeError("Missing app_id / app_secret in claude-lark-link configuration.")
        return cls(config.app_id, config.app_secret, config.base_url)

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def _refresh_tenant_token(self) -> str:
        url = f"{self._base_url}/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json={"app_id": self._app_id, "app_secret": self._app_secret})
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (0, "0", None):
                raise RuntimeError("Feishu / Lark rejected the app credentials")
            token = str(data["tenant_access_token"])
            expire = int(data.get("expire", 3600))
            self._token = _TokenState(token=token, expires_at=time.time() + max(0, expire - 60))
            return token

    async def get_tenant_access_token(self) -> str:
        if self._token.token and time.time() < self._token.expires_at:
            return self._token.token
        async with self._get_lock():
            if self._token.token and time.time() < self._token.expires_at:
                return self._token.token
            return await self._refresh_tenant_token()

    async def request(self, method: str, path: str, *, max_rate_retries: int = 3, **kwargs: Any) -> dict[str, Any]:
        if not re.fullmatch(r"/im/v1/messages(?:/om_[A-Za-z0-9]+)?", path):
            raise ValueError("Unsupported messaging endpoint")
        token = await self.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self._base_url}{path}"
        timeout = kwargs.pop("timeout", 20)

        for attempt in range(max_rate_retries + 1):
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
            if resp.status_code >= 400:
                code = None
                try:
                    payload = resp.json()
                    code = payload.get("code") if isinstance(payload, dict) else None
                except Exception:
                    pass
                if code == 99991400 and attempt < max_rate_retries:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("code") not in (0, "0", None):
                if data.get("code") == 99991400 and attempt < max_rate_retries:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise RuntimeError("Feishu / Lark rejected the messaging request")
            return data
        raise RuntimeError(f"Lark API rate limit exceeded: {path}")

    async def send_interactive(self, chat_id: str, card: dict[str, Any]) -> str:
        payload = {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
        data = await self.request("POST", "/im/v1/messages", params={"receive_id_type": "chat_id"}, json=payload)
        return str((data.get("data") or {}).get("message_id") or "")

    async def update_interactive(self, message_id: str, card: dict[str, Any]) -> None:
        if not message_id:
            return
        await self.request(
            "PATCH",
            f"/im/v1/messages/{message_id}",
            json={"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )
