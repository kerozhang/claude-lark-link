"""Standalone Python transport for the Code plugin. Reads only its local credential."""

from __future__ import annotations

import http.client
import json
import re
import sys
from hashlib import sha256
from hmac import compare_digest, digest
from pathlib import Path
from secrets import token_hex
from time import time

ROOT = Path.home() / ".claude-lark-link"
TASK_PREFIX = "[Feishu / Lark task]:\n"
# These limits match the bridge protocol. The cleaned answer uses at most four
# UTF-8 bytes per character after JSON escaping, leaving room for the envelope.
MAX_HOOK_BODY_BYTES = 64 * 1024
MAX_ANSWER_CHARS = 12000
MAX_PLAN_ITEMS = 12
MAX_PLAN_LABEL_CHARS = 800
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
EVENTS = {"UserPromptSubmit", "Stop", "StopFailure", "SessionEnd", "PostToolUse", "PostToolUseFailure",
          "PermissionRequest", "Elicitation", "Notification"}
REVIEW_INPUTS = {
    "Bash": {"command", "description", "timeout", "run_in_background", "dangerouslyDisableSandbox"},
    "PowerShell": {"command", "description", "timeout", "run_in_background", "dangerouslyDisableSandbox"},
    "ExitPlanMode": {"plan", "planFilePath", "allowedPrompts"},
    "Edit": {"file_path", "old_string", "new_string", "replace_all"},
    "Write": {"file_path", "content"},
}


def settings(root: Path) -> tuple[int, str, bytes]:
    data = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    port, prefix = data.get("port", 8765), data.get("api_prefix", "/api/v1")
    lines = (root / "plugin" / ".env").read_text(encoding="utf-8-sig").splitlines()
    tokens = [value.strip() for line in lines for name, sep, value in [line.partition("=")]
              if sep and name.strip() == "CLAUDE_LARK_HOOK_TOKEN"]
    if (type(port) is not int or not 1 <= port <= 65535 or not isinstance(prefix, str)
            or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", prefix) or not tokens
            or not re.fullmatch(r"[0-9a-f]{64}", tokens[-1])):
        raise ValueError("Invalid local bridge configuration")
    return port, prefix, bytes.fromhex(tokens[-1])


def bridge_request(root: Path, payload: dict, *, wait: bool = False) -> str | dict | None:
    port, prefix, key = settings(root)
    envelope = {**payload, "at": int(time()), "nonce": token_hex(16)}
    body = json.dumps(envelope, ensure_ascii=False).encode()
    if len(body) > MAX_HOOK_BODY_BYTES:
        return None
    signature = digest(key, b"hook:" + body, "sha256").hex()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
    try:
        connection.connect()
        connection.sock.settimeout(120 if wait else 1.5)
        connection.request("POST", f"{prefix}/hook", body, {
            "Content-Type": "application/json", "X-Claude-Lark-Signature": signature,
        })
        response = connection.getresponse()
        if response.status != 200:
            return None
        raw = response.read(48 * 1024 + 1)
        if len(raw) > 48 * 1024:
            return None
        reply = json.loads(raw)
    finally:
        connection.close()
    if not isinstance(reply, dict):
        return None
    result, proof = reply.get("result"), reply.get("proof")
    if not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof):
        return None
    if isinstance(result, str):
        material = b"decision:" + signature.encode() + result.encode()
    elif isinstance(result, dict):
        canonical = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        material = b"answers:" + signature.encode() + canonical
    else:
        return None
    return result if compare_digest(proof, digest(key, material, "sha256").hex()) else None


def review_details(tool: str, inputs: dict, mode: object) -> tuple[str, bool]:
    if tool not in REVIEW_INPUTS or not inputs:
        return "", False
    complete = not set(inputs) - REVIEW_INPUTS[tool] and mode not in ("bypassPermissions", "dontAsk")
    fields = {key: value for key, value in inputs.items() if key in REVIEW_INPUTS[tool] - {"file_path", "planFilePath"}}
    if tool in ("Bash", "PowerShell"):
        command = inputs.get("command")
        complete = (complete and isinstance(command, str) and bool(command.strip())
                    and inputs.get("dangerouslyDisableSandbox", False) is False)
    elif tool == "ExitPlanMode":
        complete = complete and isinstance(inputs.get("plan"), str) and bool(inputs["plan"].strip()) and not inputs.get("allowedPrompts")
    elif tool == "Edit":
        complete = complete and isinstance(inputs.get("old_string"), str) and isinstance(inputs.get("new_string"), str)
    elif tool == "Write":
        complete = complete and isinstance(inputs.get("content"), str)
    text = json.dumps(fields, indent=2, ensure_ascii=False)
    if len(text.encode()) > 4800:
        return "操作详情超过卡片长度限制。", False
    return text, complete


def _open_fence(text: str) -> str:
    """Return the delimiter of a code block still open at the end of text."""
    fence = ""
    for line in text.splitlines():
        match = _FENCE_OPEN.match(line)
        if not match:
            continue
        marker, info = match.groups()
        if fence:
            if marker.startswith(fence) and not info.strip():
                fence = ""
        elif len(marker) <= 16 and (marker[0] != "`" or "`" not in info):
            fence = marker
    return fence


def prepare_answer(raw: str) -> tuple[str, str]:
    cleaned = "".join(char for char in raw.replace("\r\n", "\n") if char.isprintable() or char in "\n\t").rstrip()
    if len(cleaned) <= MAX_ANSWER_CHARS:
        return cleaned, ""

    # Leave room for code fence markers and give the conclusion more space.
    head_end = 4800
    tail_start = len(cleaned) - 7100
    head_break = cleaned.rfind("\n", head_end - 160, head_end)
    tail_break = cleaned.find("\n", tail_start - 1, tail_start + 160)
    if head_break != -1:
        head_end = head_break
    if tail_break != -1 and cleaned[tail_break + 1:].strip():
        tail_start = tail_break + 1

    head, tail = cleaned[:head_end], cleaned[tail_start:]
    head_fence = _open_fence(head)
    if head_fence:
        head += "\n" + head_fence
    line_start = cleaned.rfind("\n", 0, tail_start) + 1
    tail_fence = _open_fence(cleaned[:line_start])
    if tail_fence:
        tail = tail_fence + "\n" + tail
    return head, tail


def plan_update(tool: str, inputs: dict) -> dict | None:
    if tool == "mcp__ccd_turn__wrap_up":
        return {"finish": True} if inputs.get("blocked") is not True else None
    key = "todos" if tool == "TodoWrite" else "items"
    if key in inputs:
        items = inputs[key]
        if not isinstance(items, list) or len(items) > MAX_PLAN_ITEMS:
            return None
        checked = []
        for item in items:
            if not isinstance(item, dict):
                return None
            content, status = item.get("content"), item.get("status")
            if not isinstance(content, str) or status not in ("pending", "in_progress", "completed"):
                return None
            if len(content) > MAX_PLAN_LABEL_CHARS:
                content = "任务名称过长，请在 Desktop 查看。"
            checked.append({"content": content, "status": status})
        return {"items": checked}
    if tool != "mcp__ccd_turn__update_plan":
        return None
    if inputs.get("advance") is True:
        return {"advance": True}
    update = {}
    done, start = inputs.get("done"), inputs.get("start")
    if isinstance(done, list) and len(done) <= MAX_PLAN_ITEMS and all(type(index) is int for index in done):
        update["done"] = done
    if type(start) is int:
        update["start"] = start
    return update or None


def native_event(event: dict, root: Path) -> dict:
    name = event.get("hook_event_name")
    if not isinstance(name, str) or name not in EVENTS:
        return {}
    payload = {"event": name, "session": event["session_id"], "cwd": event["cwd"], "turn_id": event.get("prompt_id")}
    tool = event.get("tool_name")
    inputs = event.get("tool_input")
    inputs = inputs if isinstance(inputs, dict) else {}
    wait = name in ("PermissionRequest", "Elicitation")
    complete = False
    if name == "UserPromptSubmit":
        prompt = event.get("prompt")
        if isinstance(prompt, str):
            prompt = prompt.replace("\r\n", "\n").strip()
            if prompt.startswith(TASK_PREFIX):
                payload["delivery"] = sha256(prompt.encode()).hexdigest()
    elif name == "Stop":
        answer = event.get("last_assistant_message")
        if isinstance(answer, str):
            head, tail = prepare_answer(answer)
            payload["answer"] = head
            payload["answer_truncated"] = bool(tail)
            if tail:
                payload["answer_tail"] = tail
    elif name == "Notification":
        payload["notification"] = event.get("notification_type")
    elif name == "Elicitation":
        payload["server"] = event.get("mcp_server_name")
        payload["message"] = event.get("message")
        if event.get("mode", "form") == "form":
            payload["schema"] = event.get("requested_schema")
    elif isinstance(tool, str):
        payload["tool"] = tool
        if name == "PostToolUse" and tool in ("mcp__ccd_turn__update_plan", "mcp__ccd_turn__wrap_up", "TodoWrite"):
            update = plan_update(tool, inputs)
            if update is None:
                return {}
            payload["plan_update"] = update
        if name == "PermissionRequest":
            if tool == "AskUserQuestion":
                payload["questions"] = inputs.get("questions")
            elif tool in REVIEW_INPUTS:
                detail, complete = review_details(tool, inputs, event.get("permission_mode"))
                payload.update(review=detail, complete=complete)
                if tool in ("Edit", "Write"):
                    payload["path"] = inputs.get("file_path")
    decision = bridge_request(root, payload, wait=wait)
    if not wait:
        return {}
    if name == "Elicitation":
        if decision == "deny":
            return {"hookSpecificOutput": {"hookEventName": name, "action": "decline"}}
        if isinstance(decision, dict) and payload.get("schema"):
            return {"hookSpecificOutput": {"hookEventName": name, "action": "accept", "content": decision}}
        return {}
    if decision == "allow" and complete:
        output = {"behavior": "allow"}
        if tool == "ExitPlanMode":
            output["updatedInput"] = inputs
    elif decision == "deny" and (tool in REVIEW_INPUTS or tool == "AskUserQuestion"):
        output = {"behavior": "deny", "message": "用户在飞书私聊卡片上拒绝了本次操作。"}
    elif tool == "AskUserQuestion" and isinstance(decision, dict) and decision:
        questions = inputs.get("questions")
        if not isinstance(questions, list) or set(decision) != {row.get("question") for row in questions if isinstance(row, dict)}:
            return {}
        if not all(isinstance(answer, str) and answer.strip() for answer in decision.values()):
            return {}
        output = {"behavior": "allow", "updatedInput": {**inputs, "answers": decision}}
    else:
        return {}
    return {"hookSpecificOutput": {"hookEventName": name, "decision": output}}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return
        result = native_event(payload, ROOT)
        if result:
            print(json.dumps(result), flush=True)
    except (OSError, http.client.HTTPException, KeyError, AttributeError, TypeError, ValueError, RecursionError):
        return


if __name__ == "__main__":
    main()
