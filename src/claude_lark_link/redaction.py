"""Secret/PII redaction and Feishu/Lark Card Markdown formatting."""

from __future__ import annotations

import re
from html import escape

_PRIVATE_ASSIGNMENT = re.compile(
    r"\b(?:[a-z0-9_-]*(?:password|passwd|secret|token|api[-_]?key)|authorization)[\"']?\s*[:=]\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|(?:(?:Basic|Bearer)\s+)?[^\s,;\"'}，；]+)",
    re.I,
)
_PRIVATE_TEXT = re.compile(
    r"https?://[^\s\"'<>]+|(?:\b[a-z]:[\\/]|\\\\)[^\s\"'<>]+|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:sk-|gh[pousr]_|github_pat_|xox[a-z]-)[A-Za-z0-9_-]+|"
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])",
    re.I,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LARK_ID_RE = re.compile(r"\b(?:ou_|cli_)[A-Za-z0-9_-]+")
_RESULT_LINK = re.compile(
    r"!?\[(?P<label>(?:\\.|[^\\\[\]\r\n])*)\]\((?P<target><[^>\r\n]*>|(?:\\.|[^\\()\r\n]|\([^()\r\n]*\))*)\)"
)
_REPO_LINK = re.compile(r"(?:\./)?[\w.-]+(?:/[\w.-]+)*/?(?:#[\w.-]+)?")
_RESULT_REFERENCE = re.compile(r"(?m)^ {0,3}\[[^\]\r\n]+\]:[ \t]*\S[^\r\n]*$")
_RESULT_TAG = re.compile(r"</?[a-zA-Z][^>\n]*>")
_REVIEW_CREDENTIAL_ARG = re.compile(
    r"(?<![\w-])--?[\w-]*(?:password|passwd|secret|token|api[-_]?key|credential|authorization)\b[ \t=]+"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;&|]+)",
    re.I,
)


def redact_text(text: str) -> str:
    text = _PRIVATE_ASSIGNMENT.sub("[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _LARK_ID_RE.sub("[ID]", text)
    return _PRIVATE_TEXT.sub("[REDACTED]", text)


def redact_credential_args(text: str) -> str:
    return redact_text(_REVIEW_CREDENTIAL_ARG.sub("[REDACTED]", text))


def display_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = redact_text(value[:900])
    text = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_CARD_MARKDOWN_CHARS = 3500
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_LANGUAGE = re.compile(r"[A-Za-z0-9_+.-]{1,32}")


def _next_fence(line: str, fence: tuple[str, str]) -> tuple[str, str]:
    match = _FENCE_LINE.match(line)
    if not match:
        return fence
    marker, info = match.groups()
    if fence[0]:
        return ("", "") if marker.startswith(fence[0]) and not info.strip() else fence
    if len(marker) > 16 or (marker[0] == "`" and "`" in info):
        return fence
    info = info.strip()
    language = info.split(maxsplit=1)[0] if info else ""
    return marker, language if _FENCE_LANGUAGE.fullmatch(language) else ""


def format_card_markdown(text: str) -> str:
    """Convert ATX headings to bold outside code fences for Feishu/Lark Markdown cards."""
    lines: list[str] = []
    fence = ("", "")
    for line in text.splitlines():
        fence = _next_fence(line, fence)
        if not fence[0]:
            match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
            if match:
                line = f"**{match[1].removeprefix('**').removesuffix('**')}**"
        lines.append(line)
    return "\n".join(lines)


def split_card_markdown(answer: str) -> list[str]:
    """Split inside the card budget while keeping fenced code readable on each page."""
    text = format_card_markdown(answer)
    if len(text) <= _CARD_MARKDOWN_CHARS:
        return [text]

    pages = []
    start = scan_from = 0
    fence = ("", "")
    while start < len(text):
        marker, language = fence
        opener = marker + language + "\n" if marker else ""
        # Leave room to close a code fence at the end of the page.
        end = min(len(text), start + _CARD_MARKDOWN_CHARS - len(opener) - 17)
        if end < len(text):
            paragraph = text.rfind("\n\n", end - 400, end)
            newline = text.rfind("\n", end - 400, end)
            if paragraph != -1:
                end = paragraph + 2
            elif newline != -1:
                end = newline + 1

        # Scan complete original lines only, even if a long line spans pages.
        scan_end = end if end == len(text) else text.rfind("\n", scan_from, end) + 1
        if scan_end > scan_from:
            for line in text[scan_from:scan_end].splitlines():
                fence = _next_fence(line, fence)
            scan_from = scan_end
        closer = ("" if text[end - 1] == "\n" else "\n") + fence[0] if fence[0] else ""
        pages.append(opener + text[start:end] + closer)
        start = end
    return pages


def sanitize_result_markdown(text: str) -> str:
    def link_text(match: re.Match[str]) -> str:
        label = match["label"]
        path = match["target"].strip().removeprefix("<").removesuffix(">")
        if _REPO_LINK.fullmatch(path) and ".." not in path.split("/"):
            return f"{label}（`{path}`）" if label and label != path else f"`{path}`"
        return label or "[REDACTED_LINK]"

    # Rewrite destinations before URL redaction can consume Markdown delimiters.
    text = _RESULT_LINK.sub(link_text, _RESULT_REFERENCE.sub("", text))
    text = redact_text(text)
    return _RESULT_TAG.sub(lambda match: escape(match[0], quote=False), text).strip()
