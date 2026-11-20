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


def format_card_markdown(text: str) -> str:
    """Convert ATX headings to bold outside code fences for Feishu/Lark Markdown cards."""
    lines: list[str] = []
    fence = ""
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1][0]
            elif fence == marker[1][0]:
                fence = ""
        if not fence and not marker:
            match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
            if match:
                line = f"**{match[1].removeprefix('**').removesuffix('**')}**"
        lines.append(line)
    return "\n".join(lines)


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
