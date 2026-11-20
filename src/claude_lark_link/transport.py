"""Receive Feishu / Lark events over the official SDK's outbound long connection."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from threading import Event, Thread
from time import monotonic, time

from .bridge import Bridge
from .cards import DESKTOP_ACTION, DESKTOP_SESSIONS_ACTION
from .inbox import MODEL_ID, desktop_inbox_command

logger = logging.getLogger(__name__)
CHAT_ID = re.compile(r"oc_[A-Za-z0-9]+")
MESSAGE_ID = re.compile(r"om_[A-Za-z0-9]+")
SESSION_ID = re.compile(r"[0-9a-f]{32}")


class LarkTransport:
    def __init__(self, bridge: Bridge, loop: asyncio.AbstractEventLoop) -> None:
        self.bridge = bridge
        self.loop = loop
        self.sdk_loop: asyncio.AbstractEventLoop | None = None
        self.client = None
        self.stop = Event()
        self.thread = Thread(target=self.run, name="lark-websocket", daemon=True)
        self.events: OrderedDict[str, float] = OrderedDict()
        self.messages: OrderedDict[str, float] = OrderedDict()

    @property
    def connected(self) -> bool:
        return bool(self.thread.is_alive() and self.client is not None and self.client._conn is not None)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.sdk_loop and self.sdk_loop.is_running():
            self.sdk_loop.call_soon_threadsafe(self.sdk_loop.stop)

    def run(self) -> None:
        from lark_oapi.core.enum import LogLevel
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        from lark_oapi.ws import client as sdk

        config = self.bridge.config
        # The SDK logs the negotiated WebSocket URL, which contains credentials.
        sdk.logger.disabled = True
        logging.getLogger("lark").setLevel(logging.CRITICAL + 1)
        loop = asyncio.new_event_loop()
        self.sdk_loop = loop
        asyncio.set_event_loop(loop)
        sdk.loop = loop

        def card(data):
            if self.stop.is_set() or self.loop.is_closed():
                return P2CardActionTriggerResponse({})
            result = asyncio.run_coroutine_threadsafe(self.on_card(data), self.loop)
            try:
                return P2CardActionTriggerResponse(result.result(timeout=1.5))
            except Exception:
                result.cancel()
                return P2CardActionTriggerResponse(self.toast(False))

        def message(data) -> None:
            if not self.stop.is_set() and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.bridge.spawn, self.on_message(data))

        try:
            handler = (EventDispatcherHandler.builder("", "")
                       .register_p2_im_message_receive_v1(message).register_p2_card_action_trigger(card).build())
            self.client = sdk.Client(config.app_id, config.app_secret, domain=config.open_platform,
                                     event_handler=handler, log_level=LogLevel.ERROR)
            if not self.stop.is_set():
                self.client.start()
        except Exception:
            if not self.stop.is_set():
                logger.error("Feishu / Lark WebSocket stopped. Check the app credentials and long-connection settings.")
        finally:
            jobs = asyncio.all_tasks(loop)
            for task in jobs:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*jobs, return_exceptions=True))
            if self.client:
                try:
                    loop.run_until_complete(asyncio.wait_for(self.client._disconnect(), timeout=2))
                except Exception:
                    pass
            loop.close()

    @staticmethod
    def fresh(ids: OrderedDict[str, float], value: str) -> bool:
        now = time()
        while ids and now - next(iter(ids.values())) > 300:
            ids.popitem(last=False)
        if not isinstance(value, str) or not value or len(value) > 128 or value in ids or len(ids) >= 8192:
            return False
        ids[value] = now
        return True

    @staticmethod
    def seconds(value) -> float | None:
        """Feishu stamps message events in milliseconds and card actions in microseconds."""
        try:
            raw = int(value)
        except (ValueError, TypeError):
            return None
        if raw >= 10**14:
            return raw / 1_000_000
        return raw / 1_000 if raw >= 10**11 else float(raw)

    @staticmethod
    def payload(value) -> dict:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return {}
        return value if isinstance(value, dict) else {}

    def accepts(self, data) -> bool:
        header = getattr(data, "header", None)
        if getattr(header, "app_id", None) != self.bridge.config.app_id:
            return False
        created = self.seconds(getattr(header, "create_time", None))
        return created is not None and -30 <= time() - created <= 120

    async def on_message(self, data) -> None:
        if not self.accepts(data):
            return
        event = data.event
        actor = getattr(getattr(getattr(event, "sender", None), "sender_id", None), "open_id", None)
        message = getattr(event, "message", None)
        chat = getattr(message, "chat_id", "") or ""
        message_id = getattr(message, "message_id", "") or ""
        if (actor != self.bridge.config.open_id or getattr(event.sender, "sender_type", None) != "user"
                or getattr(message, "chat_type", None) != "p2p" or getattr(message, "message_type", None) != "text"
                or not CHAT_ID.fullmatch(chat) or not MESSAGE_ID.fullmatch(message_id)):
            return
        if not self.fresh(self.messages, message_id) or not self.fresh(self.events, data.header.event_id):
            return
        try:
            body = json.loads(message.content)
        except (ValueError, TypeError, RecursionError):
            return
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or len(text) > 12000:
            return
        if text.strip().lower() == "/sessions":
            await self.bridge.sessions(chat)
        elif command := desktop_inbox_command(text):
            await self.bridge.command(chat, *command)

    @staticmethod
    def toast(ok: bool) -> dict:
        return {"toast": {"type": "success" if ok else "error", "content": "已收到" if ok else "请求无效或已过期"}}

    @classmethod
    def reject(cls, reason: str) -> dict:
        # The reason is a fixed code, never a user or message identifier.
        logger.warning("Claude Lark Link ignored a card action: %s", reason)
        return cls.toast(False)

    async def on_card(self, data) -> dict:
        if not self.accepts(data):
            return self.reject("stale or foreign event")
        event = data.event
        actor = getattr(getattr(event, "operator", None), "open_id", "")
        context = getattr(event, "context", None)
        chat = getattr(context, "open_chat_id", "") or ""
        message = getattr(context, "open_message_id", "") or ""
        if (actor != self.bridge.config.open_id or not CHAT_ID.fullmatch(chat) or not MESSAGE_ID.fullmatch(message)):
            return self.reject("operator or card context")
        if not self.fresh(self.events, data.header.event_id):
            return self.reject("duplicate or invalid event id")
        action = getattr(event, "action", None)
        value = self.payload(getattr(action, "value", None))
        form = self.payload(getattr(action, "form_value", None))
        choice, request = value.get("decision"), value.get("request")
        if not isinstance(choice, str) or not isinstance(request, str):
            return self.reject("button value")
        if value.get("action") == DESKTOP_ACTION:
            return self.toast(self.bridge.decide(actor, chat, message, request, choice, form))
        if value.get("action") != DESKTOP_SESSIONS_ACTION:
            return self.reject("unknown action")
        expiry, scope = self.bridge.lists.get((chat, message), (0, ""))
        if expiry <= monotonic():
            return self.reject("card not tracked or expired")
        if choice in ("recent", "all") and scope == "list":
            self.bridge.spawn(self.bridge.sessions(chat, view=choice, message=message))
            return self.toast(True)
        if choice == "model_set" and scope == request:
            values = [form.get("model_id", ""), form.get("model_pick", "")]
            if not all(isinstance(item, str) for item in values):
                return self.reject("model form")
            models = [item.strip() for item in values if item.strip()]
            if len(models) != 1 or not MODEL_ID.fullmatch(models[0]):
                return self.reject("model form")
            self.bridge.spawn(self.bridge.command(chat, "model", models[0], session=request))
            return self.toast(True)
        if choice not in ("send", "switch", "model") or scope != "list":
            return self.reject("choice")
        sid = form.get("session") if choice == "send" else request
        prompt = form.get("task") if choice == "send" else ""
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) or not isinstance(prompt, str):
            return self.reject("session or task")
        if choice == "send" and not prompt.strip():
            return self.reject("empty task")

        async def handle() -> None:
            await self.bridge.command(chat, choice, prompt.strip(), session=sid)
            if choice == "switch":
                view = value.get("view")
                await self.bridge.sessions(chat, view="all" if view == "all" else "recent", message=message)

        self.bridge.spawn(handle())
        return self.toast(True)
