"""Route local Code turns and verified private Feishu / Lark events."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from secrets import token_hex
from time import monotonic
from uuid import UUID

from .cards import (
    DesktopSessionRow,
    desktop_elicitation_card,
    desktop_input_notice,
    desktop_model_card,
    desktop_question_card,
    desktop_result_card,
    desktop_sessions_card,
    desktop_status_card,
    desktop_tool_review_card,
    notice_card,
)
from .config import BridgeConfig, TOKEN
from .inbox import DesktopDelivery, DesktopInbox, DesktopModelRequest
from .lark_client import LarkIMClient
from .protocol import (
    MAX_ANSWER_CHARS,
    NATIVE_NOTICES,
    elicitation_fields,
    parse_answers,
    parse_questions,
    safe_file,
    task_excerpt,
    valid_elicitation_content,
)
from .redaction import display_text, redact_credential_args, sanitize_result_markdown, split_card_markdown

logger = logging.getLogger(__name__)
TURN_END = {"Stop": "本轮结束", "StopFailure": "本轮异常结束", "SessionEnd": "会话已关闭"}
STEPS = {"Read": "读取文件", "Grep": "搜索内容", "Glob": "查找文件", "Edit": "修改文件", "Write": "写入文件",
         "Bash": "执行终端命令", "PowerShell": "执行终端命令", "Agent": "运行子任务", "WebFetch": "读取网页",
         "WebSearch": "搜索网页", "AskUserQuestion": "收集回答", "ExitPlanMode": "确认计划"}
REVIEW_TOOLS = {"Bash", "PowerShell", "ExitPlanMode", "Edit", "Write"}


@dataclass
class RemoteTurn:
    delivery: DesktopDelivery
    id: str
    phase: str = "运行中"
    steps: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    count: int = 0
    worker: asyncio.Task | None = None
    message: str = ""
    last_card: dict | None = None


@dataclass
class PendingReview:
    key: tuple[Path, str]
    turn: RemoteTurn
    message: str
    deadline: float
    future: asyncio.Future
    allow: bool
    questions: list[dict] | None
    fields: list[dict] | None


class Bridge:
    def __init__(self, config: BridgeConfig, im: LarkIMClient) -> None:
        self.config = config
        self.im = im
        self.inbox = DesktopInbox(config.data_dir / "paused.json")
        self.aliases = {root: alias for alias, root in config.workspaces.items()}
        self.turns: dict[tuple[Path, str], RemoteTurn] = {}
        self.reviews: dict[str, PendingReview] = {}
        self.lists: dict[tuple[str, str], tuple[float, str]] = {}
        self.background: set[asyncio.Task] = set()
        # Counts only, so a stalled plugin poll can be told apart from a missed task.
        self.stats = {"auto": 0, "poll": 0, "claim": 0, "busy": 0}

    def spawn(self, work) -> None:
        async def guarded() -> None:
            try:
                async with asyncio.timeout(150):
                    await work
            except Exception:
                logger.warning("Claude Lark Link could not complete a card operation")

        task = asyncio.create_task(guarded())
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def session_key(self, payload: dict) -> tuple[Path, str] | None:
        try:
            raw = payload["cwd"]
            if not isinstance(raw, str) or raw.startswith(("//", "\\\\")):
                return None
            root = Path(raw)
            if not root.is_absolute():
                return None
            root = root.resolve(strict=True)
            sid = UUID(payload["session"]).hex
        except (OSError, RuntimeError, KeyError, ValueError, TypeError, AttributeError):
            return None
        return (root, sid) if root in self.aliases else None

    def local_inbox(self, payload: dict) -> dict:
        key = self.session_key(payload)
        if key is None:
            return {"reason": "workspace"}
        op = payload.get("op")
        if op == "pair":
            return self.inbox.enroll(*key)
        if op in ("auto", "resume"):
            result = self.inbox.auto_enroll(*key, resume=op == "resume") if self.config.allow_auto_connect else {"reason": "auto_disabled"}
            if result.get("ticket"):
                self.stats["auto"] += 1
            return result
        ticket = payload.get("ticket")
        if not isinstance(ticket, str) or not TOKEN.fullmatch(ticket):
            return {}
        if op == "reject":
            delivery_id = payload.get("delivery_id")
            if isinstance(delivery_id, str) and re.fullmatch(r"[0-9a-f]{32}", delivery_id):
                rejected = self.inbox.reject(*key, ticket, delivery_id)
                if rejected:
                    self.spawn(self.report_rejection(rejected))
            return {}
        if op != "poll":
            return {}
        connected, item = self.inbox.poll(
            *key, ticket, ready=payload.get("ready") is True, model_capable=payload.get("model_control") is True,
            model_response=payload.get("model_response"),
        )
        self.stats["poll"] += 1
        if payload.get("ready") is not True:
            self.stats["busy"] += 1
        if isinstance(item, DesktopDelivery):
            self.stats["claim"] += 1
        if isinstance(item, DesktopModelRequest):
            return {"connected": True, "model_request": {"request": item.request, "model": item.model}}
        if item:
            item.pickup_update = asyncio.create_task(self.update_delivery(item, "Desktop 已领取 · 等待开始"))
            return {"connected": True, "task": item.text, "delivery_id": item.id}
        return {"connected": connected}

    async def report_rejection(self, delivery: DesktopDelivery) -> None:
        if delivery.pickup_update:
            await asyncio.shield(delivery.pickup_update)
        await self.update_delivery(delivery, "Desktop 未接受任务 · 未执行")

    async def update_delivery(self, delivery: DesktopDelivery, phase: str) -> None:
        message = await asyncio.shield(asyncio.wrap_future(delivery.message))
        if message:
            try:
                await self.im.update_interactive(message, self.status(delivery, phase))
            except Exception:
                logger.warning("Claude Lark Link could not update the delivery card")

    def status(self, delivery: DesktopDelivery, phase: str, turn: RemoteTurn | None = None) -> dict:
        task = display_text(task_excerpt(delivery.text), 240)
        return desktop_status_card(
            self.aliases[delivery.root], delivery.sid[:8], phase, title=display_text(task, 72), task=task,
            steps=tuple(turn.steps) if turn else (), count=turn.count if turn else 0,
        )

    async def command(self, chat: str, action: str, value: str, *, session: str = "") -> None:
        outcome = self.inbox.command(chat, action, value, set(self.aliases), session=session)
        if isinstance(outcome, str):
            if action == "unbind":
                for review in self.reviews.values():
                    if not self.review_live(review) and not review.future.done():
                        review.future.cancel()
            await self.im.send_interactive(chat, notice_card(outcome))
        elif isinstance(outcome, DesktopModelRequest):
            self.spawn(self.report_model(chat, outcome))
        else:
            message = ""
            try:
                message = await self.im.send_interactive(chat, self.status(outcome, "任务已排队 · 等待 Desktop 领取"))
            finally:
                outcome.message.set_result(message)
                self.spawn(self.expire_delivery(outcome))

    async def expire_delivery(self, delivery: DesktopDelivery) -> None:
        await asyncio.sleep(max(0, delivery.deadline - monotonic()))
        if delivery.pickup_update is None:
            await self.update_delivery(delivery, "任务未被领取 · 已过期或断开")

    def remember_card(self, chat: str, message: str, scope: str) -> None:
        if not message:
            return
        if len(self.lists) >= 64:
            self.lists.pop(next(iter(self.lists)))
        self.lists[(chat, message)] = (monotonic() + 3600, scope)

    async def report_model(self, chat: str, request: DesktopModelRequest) -> None:
        try:
            async with asyncio.timeout(max(0, request.deadline - monotonic())):
                result = await asyncio.wrap_future(request.result)
        except TimeoutError:
            result = None
        target = f"Desktop #{request.session[:8]}"
        if result and result.outcome == "current" and not request.model:
            message = await self.im.send_interactive(chat, desktop_model_card(request.session, result.model, result.options))
            self.remember_card(chat, message, request.session)
            return
        if result and result.outcome == "host_managed":
            text = f"{target} 的模型由 Claude Desktop 网关宿主管理。请在 Desktop 的模型菜单切换。"
        else:
            label = {"changed": "已切换", "unchanged": "已是所选模型"}.get(result.outcome if result else "", "未确认切换")
            text = f"{target} · {label}。"
        if result and result.model:
            text += f" 当前内部模型 ID：{display_text(result.model, 128)}。"
        await self.im.send_interactive(chat, notice_card(text))

    async def sessions(self, chat: str, *, view: str = "recent", message: str = "") -> None:
        paired, current, automatic = self.inbox.paired(chat)
        now = monotonic()
        keys = sorted(paired, key=lambda key: (key != current, self.aliases[key[0]], key[1]))
        rows = []
        for key in keys:
            root, sid = key
            turn = self.turns.get(key)
            task = display_text(task_excerpt(turn.delivery.text), 120) if turn else ""
            rows.append(DesktopSessionRow(sid[:8], self.aliases[root], f"{self.aliases[root]} · #{sid[:8]}", task,
                                          turn.phase if turn else "在线", now, sid, key == current, key in automatic))
        card = desktop_sessions_card(rows[:16 if view == "all" else 8], show_all=view == "all", now=now)
        if message:
            await self.im.update_interactive(message, card)
        else:
            message = await self.im.send_interactive(chat, card)
        self.remember_card(chat, message, "list")

    def refresh(self, turn: RemoteTurn) -> None:
        if turn.worker is None or turn.worker.done():
            turn.worker = asyncio.create_task(self.update_turn(turn))

    async def update_turn(self, turn: RemoteTurn) -> None:
        delivery = turn.delivery
        try:
            if delivery.pickup_update:
                await asyncio.shield(delivery.pickup_update)
            turn.message = turn.message or await asyncio.shield(asyncio.wrap_future(delivery.message))
            while True:
                card = self.status(delivery, turn.phase, turn)
                if card == turn.last_card:
                    return
                async with asyncio.timeout(8):
                    if turn.message:
                        await self.im.update_interactive(turn.message, card)
                    else:
                        turn.message = await self.im.send_interactive(delivery.chat, card)
                turn.last_card = card
                await asyncio.sleep(0.4)
        except Exception:
            logger.warning("Claude Lark Link could not update the turn card")

    async def send_result(self, turn: RemoteTurn, payload: dict) -> None:
        delivery = turn.delivery
        answer = payload.get("answer") if payload.get("event") == "Stop" else None
        tail = payload.get("answer_tail") if isinstance(answer, str) else None
        if isinstance(answer, str):
            answer = answer[:MAX_ANSWER_CHARS]
            remaining = MAX_ANSWER_CHARS - len(answer)
            tail = sanitize_result_markdown(tail[-remaining:]) if isinstance(tail, str) and remaining else ""
            answer = sanitize_result_markdown(answer)
        else:
            answer = ""

        pages = split_card_markdown(answer)
        tail_page = len(pages)
        if tail:
            pages.extend(split_card_markdown(tail))
        task = display_text(task_excerpt(delivery.text), 240)
        for index, text in enumerate(pages):
            notice = ""
            if tail and index == tail_page:
                notice = "中间内容已省略，以下为回复末尾。完整回答请在 Desktop 查看。"
            elif not tail and index == len(pages) - 1 and payload.get("answer_truncated") is True:
                notice = "内容较长，请到 Desktop 查看完整回答。"
            card = desktop_result_card(
                self.aliases[delivery.root], delivery.sid[:8], turn.phase, task=task if index == 0 else "",
                answer=text, notice=notice, page=f"{index + 1}/{len(pages)}" if len(pages) > 1 else "",
            )
            try:
                await self.im.send_interactive(delivery.chat, card)
            except Exception:
                # Keep trying the remaining pages so an earlier failure does not hide the conclusion.
                logger.warning("Claude Lark Link could not send result page %d/%d", index + 1, len(pages))

    def forget_turn(self, key: tuple[Path, str]) -> None:
        turn = self.turns.pop(key, None)
        for review in self.reviews.values():
            if review.turn is turn and not review.future.done():
                review.future.cancel()

    async def hook(self, payload: dict) -> str | dict | None:
        key = self.session_key(payload)
        if key is None:
            return None
        event, turn_id = payload.get("event"), payload.get("turn_id")
        if not isinstance(event, str):
            return None
        if event == "UserPromptSubmit":
            if not isinstance(turn_id, str) or not re.fullmatch(r"[a-fA-F0-9-]{36}", turn_id):
                return None
            previous = self.turns.get(key)
            if previous and previous.id == turn_id:
                return None
            self.forget_turn(key)
            fingerprint = payload.get("delivery")
            delivery = self.inbox.claim(*key, fingerprint) if isinstance(fingerprint, str) else None
            if delivery:
                turn = RemoteTurn(delivery, turn_id)
                self.turns[key] = turn
                self.refresh(turn)
            return None
        turn = self.turns.get(key)
        bound = turn is not None and self.inbox.is_paired(*key, turn.delivery.chat)
        if event == "SessionEnd":
            self.inbox.close(*key)
        if turn is None or (event != "SessionEnd" and turn_id != turn.id):
            return None
        if event in TURN_END:
            self.forget_turn(key)
            if bound:
                turn.phase = TURN_END[event]
                self.refresh(turn)
                self.spawn(self.send_result(turn, payload))
            return None
        if not self.inbox.is_paired(*key, turn.delivery.chat):
            return None
        tool = payload.get("tool")
        if event in ("PostToolUse", "PostToolUseFailure") and isinstance(tool, str) and tool in STEPS:
            turn.phase = "运行中"
            turn.steps.append(STEPS[tool] + ("（未完成）" if event.endswith("Failure") else ""))
            turn.count += 1
            self.refresh(turn)
        if event in ("PermissionRequest", "Elicitation"):
            return await self.review(key, turn, payload)
        if event == "Notification" and payload.get("notification") in NATIVE_NOTICES and turn.phase == "运行中":
            needs_input = payload.get("notification") != "permission_prompt"
            self.native_attention(key, turn, "Claude", needs_input)
        return None

    def native_attention(self, key: tuple[Path, str], turn: RemoteTurn, subject: str, needs_input: bool) -> None:
        phase = "等待 Desktop 输入" if needs_input else "等待 Desktop 授权"
        if turn.phase == phase:
            return
        turn.phase = phase
        self.refresh(turn)
        self.spawn(self.im.send_interactive(turn.delivery.chat, desktop_input_notice(
            self.aliases[key[0]], key[1][:8], subject, display_text(task_excerpt(turn.delivery.text), 180),
            input_required=needs_input,
        )))

    def review_live(self, review: PendingReview) -> bool:
        return (self.turns.get(review.key) is review.turn
                and self.inbox.is_paired(*review.key, review.turn.delivery.chat))

    def decide(self, actor: str, chat: str, message: str, request: str, choice: str, form: dict) -> bool:
        pending = self.reviews.get(request)
        if (actor != self.config.open_id or pending is None or pending.future.done() or not self.review_live(pending)
                or (chat, message) != (pending.turn.delivery.chat, pending.message) or monotonic() >= pending.deadline):
            return False
        decision: str | dict | None = None
        if choice == "deny" or (choice == "allow" and pending.allow):
            decision = choice
        elif choice == "answer" and pending.questions:
            decision = parse_answers(pending.questions, form)
        elif choice == "answer" and pending.fields:
            values = {}
            for index, row in enumerate(pending.fields):
                value = form.get(f"field_{index}", "")
                if not isinstance(value, str):
                    return False
                if not value:
                    continue
                if "options" in row:
                    choices = {str(i): item for i, item in enumerate(row["options"])}
                    if value not in choices:
                        return False
                    value = choices[value]
                values[row["key"]] = value
            if valid_elicitation_content(pending.fields, values):
                decision = values
        if decision is None:
            return False
        pending.future.set_result(decision)
        return True

    async def review(self, key: tuple[Path, str], turn: RemoteTurn, payload: dict) -> str | dict | None:
        if len(self.reviews) >= 16 or any(pending.turn is turn for pending in self.reviews.values()):
            return None
        tool = payload.get("tool")
        mcp = payload.get("event") == "Elicitation"
        questions = parse_questions(payload.get("questions")) if tool == "AskUserQuestion" else None
        fields = elicitation_fields(payload.get("schema")) if mcp else None
        details = payload.get("review")
        target = None
        allow = False
        if fields and any(redact_credential_args(text) != text for row in fields
                          for text in (row["title"], row["description"], *row.get("labels", ()),
                                       *(value for value in row.get("options", ()) if isinstance(value, str)))):
            fields = None
        if isinstance(tool, str) and tool in REVIEW_TOOLS and isinstance(details, str) and len(details) <= 4800:
            visible = redact_credential_args(details)
            allow = payload.get("complete") is True and visible == details
            if tool in ("Edit", "Write"):
                target = safe_file(key[0], payload.get("path"))
                if target is None:
                    visible = "工作区外或敏感文件的详情只在 Desktop 中显示。"
                    allow = False
        elif not questions and not mcp:
            self.native_attention(key, turn, "Claude", tool == "AskUserQuestion")
            return None
        alias, short = self.aliases[key[0]], key[1][:8]
        request = token_hex(16)
        context = dict(title="", task=display_text(task_excerpt(turn.delivery.text), 200), steps=(), count=0)
        if questions:
            shown = [dict(question=display_text(row["question"], 1000), multiSelect=row["multiSelect"],
                          labels=[display_text(label, 100) for label in row["labels"]]) for row in questions]
            if any(before["question"] != after["question"] or before["labels"] != after["labels"]
                   for before, after in zip(questions, shown)):
                self.native_attention(key, turn, "Claude 提问", True)
                return None
            card = desktop_question_card(alias, short, "待回答 · 两分钟内有效", questions=shown, request=request, **context)
        elif mcp:
            card = desktop_elicitation_card(alias, short, "MCP 等待回答 · 两分钟内有效", request=request, fields=fields,
                                            server=display_text(payload.get("server"), 140),
                                            message=display_text(payload.get("message"), 900), **context)
        else:
            review_lines = [visible] if visible else []
            if target is not None:
                review_lines.insert(0, f"文件：{target.relative_to(key[0]).as_posix()}")
            if not allow:
                review_lines.append("请在 Desktop 查看完整详情或处理批准。")
            card = desktop_tool_review_card(alias, short, "待审批 · 两分钟内有效", subject=tool,
                                            details=review_lines, allow=allow, request=request, **context)
        turn.phase = "等待飞书回答" if questions or mcp else "等待飞书审批"
        self.refresh(turn)
        pending = PendingReview(key, turn, "", monotonic() + 110, asyncio.get_running_loop().create_future(),
                                allow, questions, fields)
        self.reviews[request] = pending
        choice = None
        try:
            async with asyncio.timeout(8):
                pending.message = await self.im.send_interactive(turn.delivery.chat, card)
            if pending.message and self.review_live(pending):
                done, _ = await asyncio.wait({pending.future}, timeout=max(0, pending.deadline - monotonic()))
                if done and not pending.future.cancelled() and self.review_live(pending):
                    decision = pending.future.result()
                    if decision == "allow" and target is not None and safe_file(key[0], str(target)) != target:
                        return None
                    choice = decision
                    return choice
        except Exception:
            logger.warning("Claude Lark Link could not complete the review")
        finally:
            self.reviews.pop(request, None)
            pending.future.cancel()
            if self.turns.get(key) is turn:
                turn.phase = "运行中"
                self.refresh(turn)
            if pending.message:
                label = {"allow": "已返回本次允许", "deny": "已返回拒绝"}.get(choice if isinstance(choice, str) else "",
                                                                                       "已返回回答" if isinstance(choice, dict) else "请求已关闭")
                self.spawn(self.im.update_interactive(pending.message, notice_card(label)))
        return None
