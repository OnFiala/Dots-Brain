"""Secret-aware minimization for memories, audit records, and capture input.

Detection is deliberately conservative and catches common credential forms.  It
cannot prove that arbitrary unknown secrets are absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import InputError

_SENSITIVE_NAMES = re.compile(
    r"(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|"
    r"secret|api[-_]?key|access[-_]?token|refresh[-_]?token|token|credential)",
    re.I,
)
_ASSIGNMENT = re.compile(
    r"\b(password|passwd|secret|api[-_]?key|access[-_]?token|refresh[-_]?token|token)"
    r"\s*([=:])\s*([^\s,;]+)",
    re.I,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----", re.S
)
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.I)
_HEADER = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)"
    r"\s*:\s*[^\r\n]+",
    re.I,
)
_URL = re.compile(r"https?://[^\s'\"<>]+", re.I)


@dataclass(frozen=True)
class Sanitized:
    value: Any
    categories: dict[str, int]
    redactions: int
    truncated: dict[str, int]

    def summary(self) -> dict[str, Any]:
        return {
            "categories": self.categories,
            "redactions": self.redactions,
            "truncated": self.truncated,
        }


def _redact_text(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    def replace(category: str, pattern: re.Pattern[str], replacement: str, value: str) -> str:
        nonlocal counts

        def substitute(match):
            expanded = match.expand(replacement)
            if match.group(0) == expanded:
                return expanded
            counts[category] = counts.get(category, 0) + 1
            return expanded

        return pattern.sub(substitute, value)

    text = replace("private_key", _PRIVATE_KEY, "[REDACTED_PRIVATE_KEY]", text)
    text = replace("credential_header", _HEADER, r"\1: [REDACTED]", text)
    text = replace("token", _TOKEN, "[REDACTED_TOKEN]", text)
    text = replace("bearer", _BEARER, "Bearer [REDACTED]", text)
    text = replace("assignment", _ASSIGNMENT, r"\1\2[REDACTED]", text)

    def url_replace(match: re.Match[str]) -> str:
        value = match.group(0)
        try:
            parsed = urlsplit(value)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            changed = False
            netloc = parsed.netloc
            if parsed.username is not None or parsed.password is not None:
                netloc = netloc.rsplit("@", 1)[-1]
                changed = True
            safe = []
            for key, item in pairs:
                if _SENSITIVE_NAMES.fullmatch(key) or _SENSITIVE_NAMES.search(key):
                    safe.append((key, "[REDACTED]"))
                    changed = True
                else:
                    safe.append((key, item))
            if changed:
                counts["url_credential"] = counts.get("url_credential", 0) + 1
                return urlunsplit(
                    (parsed.scheme, netloc, parsed.path, urlencode(safe), parsed.fragment)
                )
        except ValueError:
            pass
        return value

    return _URL.sub(url_replace, text), counts


def sanitize(
    value: Any, *, max_depth: int = 8, max_items: int = 100, max_text: int = 4096
) -> Sanitized:
    """Return a bounded, JSON-safe value plus non-sensitive redaction metadata."""

    categories: dict[str, int] = {}
    truncated: dict[str, int] = {}

    def note(category: str, amount: int = 1) -> None:
        categories[category] = categories.get(category, 0) + amount

    def visit(item: Any, depth: int, key: str | None = None) -> Any:
        if depth > max_depth:
            truncated["depth"] = truncated.get("depth", 0) + 1
            return "[TRUNCATED_DEPTH]"
        if key and _SENSITIVE_NAMES.search(key):
            note("sensitive_field")
            return "[REDACTED]"
        if isinstance(item, str):
            clean, found = _redact_text(item)
            for category, count in found.items():
                note(category, count)
            if len(clean) > max_text:
                truncated["text_chars"] = truncated.get("text_chars", 0) + len(clean) - max_text
                return clean[:max_text] + "[TRUNCATED]"
            return clean
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, dict):
            output = {}
            for index, (child_key, child) in enumerate(item.items()):
                if index >= max_items:
                    truncated["items"] = truncated.get("items", 0) + len(item) - index
                    break
                name = str(child_key)
                output[name] = visit(child, depth + 1, name)
            return output
        if isinstance(item, (list, tuple)):
            output = [visit(child, depth + 1) for child in item[:max_items]]
            if len(item) > max_items:
                truncated["items"] = truncated.get("items", 0) + len(item) - max_items
            return output
        truncated["unsupported"] = truncated.get("unsupported", 0) + 1
        return f"[UNSUPPORTED_{type(item).__name__.upper()}]"

    clean = visit(value, 0)
    return Sanitized(clean, categories, sum(categories.values()), truncated)


def guard_content(mapping: dict[str, Any]) -> dict[str, Any]:
    """Fail closed for strict memory writes; error text never repeats a secret."""

    if not isinstance(mapping, dict):
        raise InputError("Content must be a structured mapping.")
    result = sanitize(mapping, max_text=32000)
    if result.redactions:
        raise InputError("Content rejected because it appears to contain a secret.")
    if result.truncated:
        raise InputError("Content exceeds the supported size or structure; split the record.")
    return result.value
