"""In-memory pairing, auto-connect, task delivery, and model-request queues."""

from __future__ import annotations

import asyncio
import json
import os
import re
from hashlib import sha256
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from threading import Lock
from time import monotonic

from .protocol import REMOTE_TASK_PREFIX, remote_task_fingerprint

_PAIR_TTL = 120
_ONLINE_TTL = 25
_TASK_TTL = 25
_DELIVERY_TTL = 3600
_MAX_SESSIONS = 16
_MAX_TASK_BYTES = 6000
MODEL_TIMEOUT = 120
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}(?:\[1m\])?", re.ASCII)
HELP = (
    "先在目标 Desktop 会话里输入 /lark-link，再私聊发送 /bind 配对码（或在插件设置中开启自动连接）；"
    "然后发 /send 任务内容、/sessions 查看会话或 /model 查看模型。"
)


def desktop_inbox_command(text: str) -> tuple[str, str] | None:
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    name = parts[0].lower()
    value = parts[1].strip() if len(parts) > 1 else ""
    if name in ("/send", "/model", "/bind", "/unbind"):
        return name[1:], value
    if name in ("/help", "/lark"):
        return "help", ""
    if name == "/desktop":
        subcommand = value.split(maxsplit=1)
        if not subcommand or subcommand[0].lower() not in ("bind", "unbind"):
            return "help", ""
        return subcommand[0].lower(), subcommand[1].strip() if len(subcommand) > 1 else ""
    return None


@dataclass
class DesktopDelivery:
    root: Path
    sid: str
    chat: str
    text: str = field(repr=False)
    deadline: float
    id: str = field(default_factory=lambda: token_hex(16))
    message: Future[str] = field(default_factory=Future, repr=False)
    pickup_update: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class DesktopModelResult:
    model: str
    options: tuple[str, ...]
    outcome: str


@dataclass
class DesktopModelRequest:
    session: str
    model: str
    deadline: float
    request: str = field(default_factory=lambda: token_hex(16))
    delivered: bool = False
    result: Future[DesktopModelResult | None] = field(default_factory=Future, repr=False)


@dataclass
class _Inbox:
    root: Path
    sid: str
    pairing_code: str = field(repr=False)
    pair_until: float
    online_until: float
    chat: str = ""
    auto: bool = False
    pending: DesktopDelivery | None = field(default=None, repr=False)
    delivered: deque[tuple[float, str, DesktopDelivery]] = field(default_factory=lambda: deque(maxlen=16), repr=False)
    model_capable: bool = False
    model_request: DesktopModelRequest | None = field(default=None, repr=False)


class DesktopInbox:
    def __init__(self, pause_file: Path | None = None) -> None:
        self._lock = Lock()
        self._sessions: dict[str, _Inbox] = {}
        self._current: dict[str, str] = {}
        self._owner_chat: str = ""
        self._pause_file = pause_file
        self._paused: set[str] = set()
        if pause_file and pause_file.exists():
            saved = json.loads(pause_file.read_text(encoding="utf-8"))
            if not isinstance(saved, list) or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) for key in saved):
                raise ValueError("invalid paused session state")
            self._paused.update(saved)

    @staticmethod
    def _pause_key(root: Path, sid: str) -> str:
        return sha256(f"{os.path.normcase(str(root))}\0{sid}".encode()).hexdigest()

    def _save_pauses(self, paused: set[str]) -> None:
        if self._pause_file:
            draft = self._pause_file.with_suffix(".tmp")
            draft.write_text(json.dumps(sorted(paused)) + "\n", encoding="utf-8")
            draft.replace(self._pause_file)
        self._paused = paused

    def is_paired(self, root: Path, sid: str, chat: str) -> bool:
        with self._lock:
            self._prune(monotonic())
            return any((row.root, row.sid, row.chat) == (root, sid, chat) for row in self._sessions.values())
    def _forget(self, ticket: str) -> None:
        inbox = self._sessions.pop(ticket)
        if inbox.model_request and not inbox.model_request.result.done():
            inbox.model_request.result.set_result(None)
        for chat, current_ticket in tuple(self._current.items()):
            if current_ticket == ticket:
                del self._current[chat]

    def _prune(self, now: float) -> None:
        for ticket, inbox in tuple(self._sessions.items()):
            if inbox.online_until <= now or (not inbox.chat and not inbox.auto and inbox.pair_until <= now):
                self._forget(ticket)

    def _attach_auto_to_chat(self, chat: str) -> None:
        """Bind pending auto-connected sessions to the verified single owner's private chat."""
        if not chat:
            return
        self._owner_chat = chat
        for ticket, row in self._sessions.items():
            if row.auto and not row.chat:
                row.chat = chat
                if chat not in self._current:
                    self._current[chat] = ticket

    def paired(self, chat: str) -> tuple[set[tuple[Path, str]], tuple[Path, str] | None, set[tuple[Path, str]]]:
        with self._lock:
            self._prune(monotonic())
            self._attach_auto_to_chat(chat)
            online = {(row.root, row.sid) for row in self._sessions.values() if row.chat == chat}
            auto_set = {(row.root, row.sid) for row in self._sessions.values() if row.chat == chat and row.auto}
            current = self._sessions.get(self._current.get(chat, ""))
            if current is None and online:
                for ticket, row in self._sessions.items():
                    if row.chat == chat:
                        self._current[chat] = ticket
                        current = row
                        break
            return online, (current.root, current.sid) if current else None, auto_set

    def enroll(self, root: Path, sid: str) -> dict[str, str]:
        with self._lock:
            now = monotonic()
            self._prune(now)
            self._remove(root, sid)
            if len(self._sessions) >= _MAX_SESSIONS:
                return {}
            ticket, code = token_hex(32), token_hex(8)
            self._sessions[ticket] = _Inbox(root, sid, code, now + _PAIR_TTL, now + _ONLINE_TTL)
            return {"ticket": ticket, "code": code}

    def auto_enroll(self, root: Path, sid: str, *, resume: bool = False) -> dict[str, object]:
        with self._lock:
            now = monotonic()
            self._prune(now)
            key = self._pause_key(root, sid)
            if key in self._paused:
                if not resume:
                    return {"paused": True}
                self._save_pauses(self._paused - {key})
            self._remove(root, sid)
            if len(self._sessions) >= _MAX_SESSIONS:
                return {}
            ticket = token_hex(32)
            chat = self._owner_chat
            self._sessions[ticket] = _Inbox(
                root, sid, "", now + _ONLINE_TTL, now + _ONLINE_TTL, chat=chat, auto=True,
            )
            if chat and self._current.get(chat) not in self._sessions:
                self._current[chat] = ticket
            return {"ticket": ticket, "auto": True}

    def poll(
        self,
        root: Path,
        sid: str,
        ticket: str,
        *,
        ready: bool,
        model_capable: bool = False,
        model_response: object = None,
    ) -> tuple[bool, DesktopDelivery | DesktopModelRequest | None]:
        with self._lock:
            now = monotonic()
            self._prune(now)
            inbox = self._sessions.get(ticket)
            if inbox is None or (inbox.root, inbox.sid) != (root, sid):
                return False, None
            inbox.online_until = now + _ONLINE_TTL
            inbox.model_capable = model_capable
            request = inbox.model_request
            if request:
                result = self._model_result(request, model_response)
                if request.deadline <= now or result:
                    inbox.model_request = None
                    if not request.result.done():
                        request.result.set_result(result if request.deadline > now else None)
                elif ready and model_capable and not request.delivered:
                    request.delivered = True
                    return True, request
                else:
                    return True, None
            if not ready:
                return True, None
            task, inbox.pending = inbox.pending, None
            if task and task.deadline > now:
                fingerprint = remote_task_fingerprint(REMOTE_TASK_PREFIX + task.text)
                inbox.delivered.append((now + _DELIVERY_TTL, fingerprint, task))
                return True, task
            return True, None

    @staticmethod
    def _model_result(request: DesktopModelRequest, response: object) -> DesktopModelResult | None:
        if not request.delivered or not isinstance(response, dict) or response.get("request") != request.request:
            return None
        model, options, outcome = response.get("model"), response.get("options"), response.get("outcome")
        if (
            not isinstance(model, str)
            or (model and not MODEL_ID.fullmatch(model))
            or not isinstance(options, list)
            or len(options) > 8
            or not all(isinstance(item, str) and MODEL_ID.fullmatch(item) for item in options)
            or outcome not in ("current", "changed", "unchanged", "failed", "host_managed")
            or (not model and outcome not in ("failed", "host_managed"))
        ):
            return None
        return DesktopModelResult(model, tuple(dict.fromkeys(options)), outcome)

    def reject(self, root: Path, sid: str, ticket: str, delivery_id: str) -> DesktopDelivery | None:
        with self._lock:
            self._prune(monotonic())
            inbox = self._sessions.get(ticket)
            if inbox is None or (inbox.root, inbox.sid) != (root, sid):
                return None
            for index, (_, _, delivery) in enumerate(inbox.delivered):
                if delivery.id == delivery_id:
                    del inbox.delivered[index]
                    return delivery
            return None

    def claim(self, root: Path, sid: str, fingerprint: str) -> DesktopDelivery | None:
        if not fingerprint:
            return None
        with self._lock:
            now = monotonic()
            self._prune(now)
            inbox = next((row for row in self._sessions.values() if (row.root, row.sid) == (root, sid)), None)
            if inbox is None:
                return None
            for index, (deadline, expected, task) in enumerate(inbox.delivered):
                if deadline > now and expected == fingerprint:
                    del inbox.delivered[index]
                    return task
            return None

    def _remove(self, root: Path, sid: str) -> None:
        for ticket, inbox in tuple(self._sessions.items()):
            if (inbox.root, inbox.sid) == (root, sid):
                self._forget(ticket)

    def close(self, root: Path, sid: str) -> None:
        with self._lock:
            self._remove(root, sid)

    def command(
        self, chat: str, action: str, value: str, allowed_roots: set[Path], *, session: str = "",
    ) -> str | DesktopDelivery | DesktopModelRequest:
        with self._lock:
            now = monotonic()
            self._prune(now)
            self._attach_auto_to_chat(chat)
            choices = {ticket: inbox for ticket, inbox in self._sessions.items() if inbox.root in allowed_roots}
            paired = {ticket: row for ticket, row in choices.items() if row.chat == chat}
            if action == "unbind":
                targets = list(paired)
                if value:
                    if not re.fullmatch(r"[0-9a-fA-F]{8,32}", value):
                        return "请输入列表中的 Desktop 会话 ID。"
                    targets = [ticket for ticket, row in paired.items() if row.sid.startswith(value.lower())]
                    if len(targets) > 1:
                        return "会话 ID 前缀不唯一，请提供更完整的 ID。"
                if not targets:
                    return "未找到已连接的 Desktop 会话。"
                paused = {self._pause_key(paired[ticket].root, paired[ticket].sid)
                          for ticket in targets if paired[ticket].auto}
                if paused:
                    self._save_pauses(self._paused | paused)
                for ticket in targets:
                    self._forget(ticket)
                return "已断开所选会话，未领取任务已取消。自动模式可在 Desktop 输入 /lark-link 恢复。"
            if action == "bind":
                if not re.fullmatch(r"[0-9a-f]{16}", value):
                    return "配对码无效。" + HELP
                for ticket, row in choices.items():
                    if not row.chat and not row.auto and row.pairing_code == value:
                        row.chat, row.pairing_code = chat, ""
                        self._current[chat] = ticket
                        return f"已关联 Desktop #{row.sid[:8]}。发 /send 任务内容即可。"
                return "配对码已用过、已过期，或 Desktop 会话已不在线。" + HELP
            if action == "send" and not value:
                return "请输入 /send 任务内容。"
            if action in ("send", "switch", "model"):
                if session:
                    ticket = next((key for key, row in paired.items() if row.sid == session), "")
                else:
                    ticket = self._current.get(chat, "")
                    if ticket not in paired and paired:
                        ticket = next(iter(paired))
                        self._current[chat] = ticket
                bound = paired.get(ticket)
                if bound is None:
                    return "目标 Desktop 会话未连接或已离线。" if session else "没有在线的已连接 Desktop 会话。" + HELP
                if action == "switch":
                    self._current[chat] = ticket
                    return f"已切换到 Desktop #{bound.sid[:8]}。"
                if action == "model":
                    if value and not MODEL_ID.fullmatch(value):
                        return "请输入 Desktop 模型选择器里的模型 ID，例如 /model sonnet。"
                    if not bound.model_capable:
                        return "当前 Desktop 插件尚未报告模型切换能力。请确认已在 Claude Desktop 中运行最新版插件。"
                    if bound.model_request:
                        if bound.model_request.deadline > now:
                            return "Desktop 正在处理上一条模型请求。"
                        if not bound.model_request.result.done():
                            bound.model_request.result.set_result(None)
                    if value and bound.pending and bound.pending.deadline > now:
                        return "该会话已有任务排队，请等任务开始后再切换。"
                    bound.model_request = DesktopModelRequest(bound.sid, value, now + MODEL_TIMEOUT)
                    return bound.model_request
                if len(value.encode("utf-8")) > _MAX_TASK_BYTES or "\x00" in value:
                    return "任务太长或包含不支持的字符，请用简短的纯文本重试。"
                if bound.pending and bound.pending.deadline > now:
                    return "上一条任务还没被 Desktop 领取，请稍后再发。"
                bound.pending = DesktopDelivery(bound.root, bound.sid, chat, value, now + _TASK_TTL)
                return bound.pending
            return HELP
