"""Shared hook / inbox protocol constants and validation helpers."""

from __future__ import annotations

import json
import re
import stat
from hashlib import sha256
from hmac import digest
from pathlib import Path
from typing import Any

from .redaction import display_text, redact_credential_args

DECISION_EVENTS = {"PermissionRequest", "Elicitation"}
NATIVE_NOTICES = ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog")
REMOTE_TASK_PREFIX = "[Feishu / Lark task]:\n"
MAX_HOOK_BODY_BYTES = 64 * 1024
# Keep in sync with the standalone plugin hook's answer limit.
MAX_ANSWER_CHARS = 12000
SIGNATURE_HEADER = "X-Claude-Lark-Signature"
INBOX_HEADER = "X-Claude-Lark-Inbox"
PLAN_TOOLS = {"mcp__ccd_turn__update_plan", "mcp__ccd_turn__wrap_up", "TodoWrite"}
MAX_PLAN_ITEMS = 12
PlanItem = tuple[str, str]

_PROMPT_BLOCK = re.compile(
    r"```[\s\S]*?(?:```|\Z)|<pasted_content\b[^>]*>[\s\S]*?(?:</pasted_content>|\Z)|"
    r"^[ \t]*<(?P<internal>task-notification|system-reminder|side-sessions-event|ci-monitor-event)(?:\s[^<>]*)?>"
    r"[\s\S]*?(?:</(?P=internal)\s*>|\Z)",
    re.IGNORECASE | re.MULTILINE,
)
_PRIVATE_FIELD = re.compile(
    r"password|passwd|secret|token|api[ _-]?key|credential|authorization|验证码|密码|密钥|令牌", re.I,
)
_SENSITIVE_PATH = re.compile(
    r"(^\.|^logs$|^data$|^venv$|^node_modules$|secret|credential|id_rsa|\.(?:pem|key|pfx|p12)$)", re.I,
)


def remote_task_fingerprint(prompt: str) -> str:
    normalized = prompt.replace("\r\n", "\n").strip()
    if not normalized.startswith(REMOTE_TASK_PREFIX):
        return ""
    return sha256(normalized.encode("utf-8")).hexdigest()


def task_excerpt(value: object) -> str | None:
    """Return the displayable input, or None for an internal-only notification."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    excerpt = _PROMPT_BLOCK.sub(
        lambda match: "" if match.group("internal") else "[已省略粘贴内容]", text,
    ).strip()
    return excerpt[:600] if excerpt or not text else None


def apply_plan_update(current: tuple[PlanItem, ...], update: object) -> tuple[PlanItem, ...]:
    """Apply one successful Desktop task-list update without inventing any steps."""
    if not isinstance(update, dict):
        return current
    if "items" in update:
        raw = update["items"]
        if not isinstance(raw, list) or len(raw) > MAX_PLAN_ITEMS:
            return current
        items = []
        for row in raw:
            if not isinstance(row, dict):
                return current
            content, status = row.get("content"), row.get("status")
            if (not isinstance(content, str) or len(content) > 800
                    or status not in ("pending", "in_progress", "completed")):
                return current
            content = display_text(redact_credential_args(content), 240)
            items.append((content or "未命名任务", status))
        return tuple(items)
    if not current:
        return current
    if update.get("finish") is True:
        return tuple((content, "completed") for content, _ in current)

    items = list(current)
    if update.get("advance") is True:
        active = next((index for index, (_, status) in enumerate(items) if status == "in_progress"), None)
        if active is not None:
            items[active] = (items[active][0], "completed")
        start = next((index + 1 for index, (_, status) in enumerate(items) if status == "pending"), None)
    else:
        done, start = update.get("done", []), update.get("start")
        if (not isinstance(done, list) or len(done) > MAX_PLAN_ITEMS
                or any(type(index) is not int or not 1 <= index <= len(items) for index in done)
                or start is not None and (type(start) is not int or not 1 <= start <= len(items))):
            return current
        for index in done:
            items[index - 1] = (items[index - 1][0], "completed")
    if start is not None:
        for index, (content, status) in enumerate(items, 1):
            if index == start:
                items[index - 1] = (content, "in_progress")
            elif status == "in_progress":
                items[index - 1] = (content, "pending")
    return tuple(items)


def decision_proof(key: bytes, signature: bytes, decision: str | dict) -> str:
    if isinstance(decision, str):
        return digest(key, b"decision:" + signature + decision.encode(), "sha256").hex()
    body = json.dumps(decision, sort_keys=True, separators=(",", ":")).encode()
    return digest(key, b"answers:" + signature + body, "sha256").hex()


def safe_file(root: Path, value: Any) -> Path | None:
    """Validate target file within workspace; reject symlinks, hardlinks, and sensitive paths."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        path = Path(value)
        if any(part == ".." for part in path.parts):
            return None
        path = path if path.is_absolute() else root / path
        relative = path.relative_to(root)
        if not relative.parts:
            return None
        candidate = root
        for part in relative.parts:
            if _SENSITIVE_PATH.search(part) or ":" in part or part.rstrip(" .") != part:
                return None
            if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", part, re.I):
                return None
            candidate = candidate / part
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
                return None
        resolved = candidate.resolve()
        if resolved != candidate or not resolved.is_relative_to(root):
            return None
        if resolved.exists() and (not resolved.is_file() or resolved.stat().st_nlink != 1):
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def parse_questions(value: Any) -> list[dict] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        return None
    parsed = []
    for item in value:
        if not isinstance(item, dict):
            return None
        question = item.get("question")
        options = item.get("options")
        if (
            not isinstance(question, str)
            or not 1 <= len(question) <= 1000
            or not isinstance(options, list)
            or not 1 <= len(options) <= 6
        ):
            return None
        labels = [option.get("label") if isinstance(option, dict) else None for option in options]
        if not all(isinstance(label, str) and 1 <= len(label) <= 100 for label in labels):
            return None
        parsed.append({"question": question, "labels": labels, "multiSelect": item.get("multiSelect") is True})
    return parsed if len({item["question"] for item in parsed}) == len(parsed) else None


def parse_answers(questions: list[dict], form: dict) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for i, question in enumerate(questions):
        custom = form.get(f"text_{i}", "")
        picked = form.get(f"pick_{i}")
        if not isinstance(custom, str) or len(custom) > 2000:
            return None
        picks = [picked] if isinstance(picked, str) and picked else (picked if isinstance(picked, list) else [])
        labels = question["labels"]
        valid = {str(j): label for j, label in enumerate(labels)}
        if (
            picked not in (None, "", [])
            and not picks
            or any(not isinstance(p, str) or p not in valid for p in picks)
            or len(set(picks)) != len(picks)
            or (not question["multiSelect"] and len(picks) > 1)
        ):
            return None
        if bool(custom.strip()) == bool(picks):
            return None
        result[question["question"]] = custom.strip() if custom.strip() else ", ".join(valid[p] for p in picks)
    return result


def elicitation_fields(schema: object) -> list[dict] | None:
    """Return the form fields whose constraints can be represented by a Feishu/Lark form."""
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or set(schema) - {"type", "properties", "required", "additionalProperties", "$schema", "title", "description"}
    ):
        return None
    properties = schema.get("properties")
    required = schema.get("required", [])
    if (
        not isinstance(properties, dict)
        or not 1 <= len(properties) <= 6
        or not isinstance(required, list)
        or any(not isinstance(key, str) or key not in properties for key in required)
    ):
        return None
    fields: list[dict] = []
    for key, field_def in properties.items():
        if not isinstance(field_def, dict) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", key):
            return None
        kind = field_def.get("type")
        title, description = field_def.get("title", key), field_def.get("description", "")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 100
            or not isinstance(description, str)
            or len(description) > 300
            or _PRIVATE_FIELD.search(f"{key} {title} {description}")
        ):
            return None
        row: dict[str, Any] = {"key": key, "title": title, "description": description, "required": key in required}
        extras = set(field_def) - {"type", "title", "description", "default"}
        if kind == "boolean" and not extras:
            row.update(options=[True, False], labels=["是 / Yes", "否 / No"])
        elif kind == "string" and "enum" in field_def and not extras - {"enum", "enumNames"}:
            options = field_def["enum"]
            labels = field_def.get("enumNames", options)
            if (
                not isinstance(options, list)
                or not isinstance(labels, list)
                or not 1 <= len(options) == len(labels) <= 10
                or any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in options + labels)
                or len(set(options)) != len(options)
                or len(set(labels)) != len(labels)
            ):
                return None
            row.update(options=options, labels=labels)
        elif kind == "string" and not extras - {"minLength", "maxLength"}:
            minimum, maximum = field_def.get("minLength", 0), field_def.get("maxLength", 500)
            if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= min(maximum, 500):
                return None
            row.update(minimum=minimum, maximum=min(maximum, 500))
        else:
            return None
        fields.append(row)
    return fields


def valid_elicitation_content(fields: list[dict], content: object) -> bool:
    if not isinstance(content, dict) or set(content) - {row["key"] for row in fields}:
        return False
    for row in fields:
        key = row["key"]
        if key not in content:
            if row["required"]:
                return False
            continue
        value = content[key]
        if "options" in row:
            if not any(type(value) is type(option) and value == option for option in row["options"]):
                return False
        elif (
            not isinstance(value, str)
            or not row["minimum"] <= len(value) <= row["maximum"]
            or any(not char.isprintable() for char in value)
        ):
            return False
    return True
