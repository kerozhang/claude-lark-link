"""Authenticated, loopback-only API used by the local plugin."""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from hmac import compare_digest, digest
from time import time

from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from .bridge import Bridge
from .config import BridgeConfig
from .lark_client import LarkIMClient
from .protocol import DECISION_EVENTS, MAX_HOOK_BODY_BYTES, SIGNATURE_HEADER, decision_proof


def create_app(config: BridgeConfig, *, im: LarkIMClient | None = None, connect: bool = True) -> FastAPI:
    bridge = Bridge(config, im or LarkIMClient.from_config(config))
    key = bytes.fromhex(config.hook_token)
    seen: OrderedDict[str, float] = OrderedDict()
    transport = None

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal transport
        if connect:
            from .transport import LarkTransport

            transport = LarkTransport(bridge, asyncio.get_running_loop())
            transport.start()
        try:
            yield
        finally:
            if transport:
                transport.close()
            jobs = set(bridge.background)
            jobs.update(turn.worker for turn in bridge.turns.values() if turn.worker is not None)
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bridge = bridge

    @app.get(config.api_prefix + "/healthz")
    async def health() -> dict:
        return {"status": "ok", "lark_connected": bool(transport and transport.connected), "stats": dict(bridge.stats)}

    async def receive(request: Request, purpose: bytes, limit: int) -> tuple[dict, bytes] | JSONResponse:
        headers = request.headers
        if (not config.enabled or request.client is None or request.client.host != "127.0.0.1"
                or headers.get("host") != f"127.0.0.1:{config.port}"
                or headers.get("content-type", "").partition(";")[0] != "application/json"
                or any(name in headers for name in ("origin", "forwarded", "x-forwarded-host", "x-forwarded-for"))):
            return JSONResponse({}, status_code=403)
        signature = headers.get(SIGNATURE_HEADER, "").encode()
        if len(signature) != 64:
            return JSONResponse({}, status_code=403)
        raw = bytearray()
        try:
            async with asyncio.timeout(2):
                async for part in request.stream():
                    if len(raw) + len(part) > limit:
                        return JSONResponse({}, status_code=413)
                    raw.extend(part)
            expected = digest(key, purpose + bytes(raw), "sha256").hex().encode()
            if not compare_digest(signature, expected):
                return JSONResponse({}, status_code=403)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return JSONResponse({}, status_code=400)
            nonce, timestamp = payload.get("nonce"), payload.get("at")
            now = time()
            if (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
                    or type(timestamp) is not int or abs(now - timestamp) > 30):
                return JSONResponse({}, status_code=403)
            while seen and now - next(iter(seen.values())) > 61:
                seen.popitem(last=False)
            if nonce in seen:
                return JSONResponse({}, status_code=409)
            if len(seen) >= 16384:
                return JSONResponse({}, status_code=429)
            seen[nonce] = now
            return payload, signature
        except (TimeoutError, ClientDisconnect, ValueError, RecursionError):
            return JSONResponse({}, status_code=400)

    def signed(signature: bytes, result: str | dict) -> JSONResponse:
        return JSONResponse({"result": result, "proof": decision_proof(key, signature, result)},
                            headers={"Cache-Control": "no-store"})

    @app.post(config.api_prefix + "/inbox")
    async def inbox(request: Request) -> JSONResponse:
        received = await receive(request, b"inbox:", 4096)
        if isinstance(received, JSONResponse):
            return received
        payload, signature = received
        result = json.dumps(bridge.local_inbox(payload), ensure_ascii=True, separators=(",", ":"))
        proof = digest(key, b"inbox-result:" + signature + result.encode(), "sha256").hex()
        return JSONResponse({"result": result, "proof": proof}, headers={"Cache-Control": "no-store"})

    @app.post(config.api_prefix + "/hook")
    async def hook(request: Request) -> JSONResponse:
        received = await receive(request, b"hook:", MAX_HOOK_BODY_BYTES)
        if isinstance(received, JSONResponse):
            return received
        payload, signature = received
        if not isinstance(payload.get("event"), str) or payload["event"] not in DECISION_EVENTS:
            await bridge.hook(payload)
            return signed(signature, {})
        work = asyncio.create_task(bridge.hook(payload))
        disconnect = asyncio.create_task(request.receive())
        try:
            done, _ = await asyncio.wait({work, disconnect}, return_when=asyncio.FIRST_COMPLETED)
            result = work.result() if work in done and disconnect not in done else None
            return signed(signature, result) if result is not None else JSONResponse({})
        finally:
            work.cancel()
            disconnect.cancel()
            await asyncio.gather(work, disconnect, return_exceptions=True)

    return app
