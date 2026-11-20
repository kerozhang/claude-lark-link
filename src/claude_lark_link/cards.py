"""Feishu / Lark Card 2.0 builders for claude-lark-link."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .redaction import format_card_markdown

DESKTOP_ACTION = "claude_lark_link_approval"
DESKTOP_SESSIONS_ACTION = "claude_lark_link_sessions"
BRAND = "Claude Lark Link"


def _plain(text: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": text}


def _line(text: str) -> dict[str, Any]:
    return {"tag": "div", "text": _plain(text)}


def _card(title: str, elements: list[dict[str, Any]], color: str = "blue", *, subtitle: str = "") -> dict[str, Any]:
    header: dict[str, Any] = {"title": _plain(title), "template": color}
    if subtitle:
        header["subtitle"] = _plain(subtitle)
    return {"schema": "2.0", "config": {"update_multi": True}, "header": header, "body": {"elements": elements}}


def _button(
    label: str,
    request: str,
    decision: str,
    *,
    submit: bool = False,
    action: str = DESKTOP_ACTION,
) -> dict[str, Any]:
    button: dict[str, Any] = {
        "tag": "button",
        "text": _plain(label),
        "type": "default" if decision == "deny" else "primary",
        "behaviors": [{"type": "callback", "value": {"action": action, "request": request, "decision": decision}}],
    }
    if submit:
        button.update(name="submit_claude_lark_form", form_action_type="submit")
    return button


def notice_card(label: str) -> dict[str, Any]:
    return _card(BRAND, [_line(label)])


def _desktop_context(task: str, steps: tuple[str, ...], count: int) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    if task:
        elements.extend([{"tag": "markdown", "content": "**本轮任务**"}, _line(task)])
    if steps:
        first = count - len(steps) + 1
        numbered = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, first))
        elements.extend([{"tag": "markdown", "content": f"**最近步骤 · 共 {count} 步**"}, _line(numbered)])
    return elements


def desktop_status_card(
    alias: str, session: str, phase: str, *, title: str, task: str, steps: tuple[str, ...], count: int,
) -> dict[str, Any]:
    colors = {
        "等待飞书审批": "orange",
        "等待 Desktop 授权": "orange",
        "等待飞书回答": "turquoise",
        "等待 Desktop 输入": "turquoise",
        "本轮结束": "green",
        "本轮异常结束": "red",
        "会话已关闭": "grey",
    }
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": f"**{phase}**"}]
    elements.extend(_desktop_context(task, steps, count))
    return _card(
        title or "Claude Desktop 会话",
        elements,
        colors.get(phase, "blue"),
        subtitle=f"{BRAND} · {alias} · #{session}",
    )


def desktop_result_card(
    alias: str, session: str, phase: str, *, task: str, answer: str, truncated: bool = False,
) -> dict[str, Any]:
    if phase == "本轮异常结束":
        label, color = "任务异常结束", "red"
        answer = "请在 Claude Desktop 中查看错误详情。"
    elif phase == "会话已关闭":
        label, color = "会话已关闭", "grey"
        answer = "会话关闭前未收到任务结果。"
    else:
        label, color = "任务已完成", "green"
        answer = answer or "本轮未返回可同步的文本，请在 Claude Desktop 中查看。"
    elements: list[dict[str, Any]] = [_line(task)] if task else []
    elements.append({"tag": "markdown", "content": format_card_markdown(answer)})
    if truncated:
        elements.append(_line("内容较长，请到 Desktop 查看完整回答。"))
    return _card(f"Desktop {label}", elements, color, subtitle=f"{BRAND} · {alias} · #{session}")


@dataclass(frozen=True)
class DesktopSessionRow:
    session: str
    workspace: str
    title: str
    task: str
    phase: str
    updated_at: float
    target: str = ""
    current: bool = False
    auto: bool = False


def desktop_sessions_card(rows: list[DesktopSessionRow], *, show_all: bool, now: float) -> dict[str, Any]:
    scope = "全部在线会话" if show_all else "在线会话 · 最多 8 个"
    elements: list[dict[str, Any]] = [
        _button("刷新", "list", "all" if show_all else "recent", action=DESKTOP_SESSIONS_ACTION),
        _button(
            "只看最近" if show_all else "更多会话",
            "list",
            "recent" if show_all else "all",
            action=DESKTOP_SESSIONS_ACTION,
        ),
    ]
    if not rows:
        elements.append(_line("暂无已同步的会话。在已授权工作区打开 Claude Desktop 会话后，点「刷新」查看。"))
    current = next((row for row in rows if row.current), None)
    if current:
        elements.append(_button("切换模型", current.target, "model", action=DESKTOP_SESSIONS_ACTION))
    choices = []
    view = "all" if show_all else "recent"
    for row in rows:
        age = max(0, int(now - row.updated_at))
        last_seen = "刚刚" if age < 60 else f"{age // 60} 分钟前" if age < 3600 else f"{age // 3600} 小时前"
        if row.current:
            status = " · 当前（自动）" if row.auto else " · 当前"
        elif row.target:
            status = " · 自动连接" if row.auto else " · 已配对"
        else:
            status = ""
        lines = [row.title, f"{row.phase} · {row.workspace} · #{row.session} · {last_seen}{status}"]
        if row.task and row.task != row.title:
            lines.append(f"最近任务：{row.task}")
        elements.extend([{"tag": "hr"}, _line("\n".join(lines))])
        if row.target:
            choices.append({"text": _plain(f"{row.title} · #{row.session}"), "value": row.target})
            if not row.current:
                button = _button("设为当前", row.target, "switch", action=DESKTOP_SESSIONS_ACTION)
                button["behaviors"][0]["value"]["view"] = view
                elements.append(button)
    if choices:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "form",
                "name": "desktop_send",
                "elements": [
                    {"tag": "select_static", "name": "session", "placeholder": _plain("选择要发送的会话"), "options": choices},
                    {"tag": "input", "name": "task", "placeholder": _plain("输入任务")},
                    _button("发送", "list", "send", submit=True, action=DESKTOP_SESSIONS_ACTION),
                ],
            },
        ])
    elif rows:
        elements.append(_line("在 Desktop 会话运行 /lark-link 配对，或在插件设置中开启「自动连接」后，即可从此卡发任务。"))
    elements.append(_line("仅显示当前已连接的会话。"))
    return _card("Claude Desktop 会话", elements, subtitle=f"{BRAND} · {scope} · 已显示 {len(rows)} 个")


def desktop_model_card(session: str, current: str, options: tuple[str, ...]) -> dict[str, Any]:
    inputs: list[dict[str, Any]] = []
    if options:
        inputs.append({
            "tag": "select_static",
            "name": "model_pick",
            "placeholder": _plain("选择 Desktop 提供的模型"),
            "options": [{"text": _plain(model), "value": model} for model in options],
        })
    inputs.append({
        "tag": "input",
        "name": "model_id",
        "placeholder": _plain("或填写模型 ID" if options else "填写 Desktop 模型 ID"),
    })
    inputs.append(_button("切换", session, "model_set", submit=True, action=DESKTOP_SESSIONS_ACTION))
    return _card(
        "Claude Desktop 模型",
        [_line(f"当前模型：{current}"), {"tag": "form", "name": "desktop_model", "elements": inputs}],
        subtitle=f"{BRAND} · Desktop #{session[:8]}",
    )


def _question_form(request: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        elements.append(_line(question["question"]))
        elements.append({
            "tag": "multi_select_static" if question["multiSelect"] else "select_static",
            "name": f"pick_{index}",
            "placeholder": _plain("请选择，或在下方填写"),
            "options": [{"text": _plain(label), "value": str(i)} for i, label in enumerate(question["labels"])],
        })
        elements.append({"tag": "input", "name": f"text_{index}", "placeholder": _plain("其他答案（可选）")})
    elements.append(_button("提交回答", request, "answer", submit=True, action=DESKTOP_ACTION))
    return {"tag": "form", "name": "claude_questions", "elements": elements}


def desktop_question_card(
    alias: str, session: str, label: str, *, title: str, task: str, steps: tuple[str, ...], count: int,
    questions: list[dict[str, Any]], request: str = "",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [_line(label), *_desktop_context(task, steps, count)]
    if request:
        elements.extend([
            _question_form(request, questions),
            _button("取消回答", request, "deny", action=DESKTOP_ACTION),
        ])
    else:
        elements.extend(_line(question["question"]) for question in questions)
    return _card(
        title or "Claude 等待回答",
        elements,
        "turquoise" if request else "grey",
        subtitle=f"{BRAND} · Claude 提问 · {alias} · #{session}",
    )


def desktop_tool_review_card(
    alias: str, session: str, label: str, *, title: str, task: str, steps: tuple[str, ...], count: int,
    subject: str, details: list[str], allow: bool, request: str = "",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [_line(label), *_desktop_context(task, steps, count), {"tag": "hr"}]
    elements.extend(_line(detail) for detail in details)
    if request:
        if allow:
            elements.append(_button("仅允许本次", request, "allow", action=DESKTOP_ACTION))
        elements.append(_button("拒绝", request, "deny", action=DESKTOP_ACTION))
    color = "orange" if request else {"已返回本次允许": "green", "已返回拒绝": "red"}.get(label, "grey")
    return _card(title or subject, elements, color, subtitle=f"{BRAND} · {subject} · {alias} · #{session}")


def desktop_elicitation_card(
    alias: str, session: str, label: str, *, title: str, task: str, steps: tuple[str, ...], count: int,
    server: str, message: str, fields: list[dict[str, Any]] | None, request: str = "",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        _line(label),
        *_desktop_context(task, steps, count),
        _line(f"MCP 服务：{server}\n{message}"),
    ]
    if request and fields:
        inputs: list[dict[str, Any]] = []
        for index, field in enumerate(fields):
            requirement = "（必填）" if field["required"] else "（选填）"
            inputs.append(_line(f"{field['title']}{requirement}\n{field['description']}".rstrip()))
            name = f"field_{index}"
            if "labels" in field:
                inputs.append({
                    "tag": "select_static",
                    "name": name,
                    "placeholder": _plain("请选择"),
                    "options": [{"text": _plain(text), "value": str(option)} for option, text in enumerate(field["labels"])],
                })
            else:
                inputs.append({"tag": "input", "name": name, "placeholder": _plain("请输入")})
        inputs.append(_button("提交回答", request, "answer", submit=True, action=DESKTOP_ACTION))
        elements.append({"tag": "form", "name": "mcp_elicitation", "elements": inputs})
    if request:
        if not fields:
            elements.append(_line("这类请求只能在飞书拒绝。"))
        elements.append(_button("拒绝请求", request, "deny", action=DESKTOP_ACTION))
    return _card(
        title or "MCP 等待回答",
        elements,
        "turquoise" if request else "grey",
        subtitle=f"{BRAND} · MCP 输入 · {alias} · #{session}",
    )


def desktop_input_notice(
    alias: str, session: str, subject: str, task: str, *, input_required: bool,
) -> dict[str, Any]:
    action = "输入" if input_required else "授权"
    elements: list[dict[str, Any]] = [_line(f"Claude Desktop 正在等待{action}。桥接仅收到了 {subject} 的通知。")]
    if task:
        elements.append(_line(task))
    return _card(
        f"等待 Desktop {action}",
        elements,
        "turquoise" if input_required else "orange",
        subtitle=f"{BRAND} · {alias} · #{session}",
    )


def desktop_approval_card(
    alias: str, session: str, phase: str, *, title: str, task: str, steps: tuple[str, ...], count: int,
    path: str, change: str, request: str = "",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{phase}**"},
        _line(f"{change}\n{path}"),
    ]
    context = _desktop_context(task, steps, count)
    elements.extend([{"tag": "hr"}, *(context or [_line("本轮任务尚未同步，请在 Desktop 核对。")])])
    if request:
        elements.extend([
            {"tag": "hr"},
            _line("查看完整改动可打开 Desktop 的审批窗口。"),
            _button("仅允许本次", request, "allow", action=DESKTOP_ACTION),
            _button("拒绝", request, "deny", action=DESKTOP_ACTION),
        ])
    color = "orange" if request else {"已返回本次允许": "green", "已返回拒绝": "red"}.get(phase, "grey")
    return _card(title or "文件审批", elements, color, subtitle=f"{BRAND} · 文件审批 · {alias} · #{session}")
