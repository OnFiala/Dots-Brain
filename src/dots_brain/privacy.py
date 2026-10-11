"""Secret-aware minimization for memories, audit records, and capture input.

Detection is deliberately conservative and catches common credential forms.  It
cannot prove that arbitrary unknown secrets are absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import InputError

_SENSITIVE_NAMES = re.compile(
    r"(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|"
    r"secret|api[-_]?key|access[-_]?token|refresh[-_]?token|token|credential)",
    re.I,
)
_ASSIGNMENT = re.compile(
    r"(?<![^\W_])(password|passwd|secret|api[-_]?key|access[-_]?token|refresh[-_]?token|token)"
    r"\s*+([=:])\s*+([^\s,;]++)",
    re.I,
)
_PRIVATE_KEY = re.compile(r"-----BEGIN ([^-\n]*PRIVATE KEY)-----")
_TOKEN = re.compile(
    r"(?<![^\W_])(?:gh[pousr]_[A-Za-z0-9_]{20,}+|github_pat_[A-Za-z0-9_]{20,}+|"
    r"sk-[A-Za-z0-9_-]{16,}+|AKIA[0-9A-Z]{16})|"
    r"(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]{12,}+\.[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+",
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.I)
_HEADER = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)"
    r"\s*+:\s*+([^\r\n]++)",
    re.I,
)
_URL = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*+://[^\s'\"<>]++")


def sensitive_name(name: str) -> bool:
    name = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower()
    return bool(re.search(r"(?:^|[-_])" + _SENSITIVE_NAMES.pattern + r"$", name))


def certain(value: str, *, name: str = "", literal: bool = False) -> bool | None:
    value = value.strip().strip("\"'")
    if not value or value.startswith(("[REDACTED", "${", "<")):
        return None
    if name.lower() == "token":
        return False  # Parser tokens and modes do not establish a credential.
    return literal or (
        not any(char.isspace() for char in value)
        and (
            any(char.isdigit() or not char.isalpha() for char in value)
            or len(value) >= 8
            and not value.islower()
        )
    )


@dataclass(frozen=True)
class Sanitized:
    value: Any
    categories: dict[str, int]
    redactions: int
    truncated: dict[str, int]
    warnings: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "categories": self.categories,
            "redactions": self.redactions,
            "truncated": self.truncated,
        }


def _redact_text(text: str, *, redact_uncertain=True):
    counts: dict[str, int] = {}
    warnings: dict[str, int] = {}

    def replace(category: str, pattern: re.Pattern[str], replacement: str, value: str) -> str:
        nonlocal counts

        def substitute(match):
            if category in {"assignment", "credential_header"}:
                if category == "assignment":
                    confidence = certain(match[3], name=match[1])
                else:
                    value = match[2]
                    confidence = certain(
                        value,
                        literal=(
                            value.lower().startswith(("basic ", "bearer "))
                            or "cookie" in match[1].lower()
                            and "=" in value
                        ),
                    )
                if confidence is None:
                    return match[0]
                if not confidence:
                    warnings[category] = warnings.get(category, 0) + 1
                    if not redact_uncertain:
                        return match[0]
            expanded = match.expand(replacement)
            if match.group(0) == expanded:
                return expanded
            counts[category] = counts.get(category, 0) + 1
            return expanded

        return pattern.sub(substitute, value)

    # Consume each PEM block once, including an unterminated final block.
    pieces, cursor = [], 0
    while match := _PRIVATE_KEY.search(text, cursor):
        end_marker = "-----END " + match[1] + "-----"
        end = text.find(end_marker, match.end())
        pieces.extend((text[cursor : match.start()], "[REDACTED_PRIVATE_KEY]"))
        cursor = len(text) if end < 0 else end + len(end_marker)
        counts["private_key"] = counts.get("private_key", 0) + 1
    text = "".join(pieces) + text[cursor:]
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
                if sensitive_name(key):
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

    return _URL.sub(url_replace, text), counts, warnings


def sanitize(
    value: Any,
    *,
    max_depth: int = 8,
    max_items: int = 100,
    max_text: int = 4096,
    _redact_uncertain: bool = True,
) -> Sanitized:
    """Return a bounded, JSON-safe value plus non-sensitive redaction metadata."""

    categories: dict[str, int] = {}
    truncated: dict[str, int] = {}
    warnings: dict[str, int] = {}

    def note(category: str, amount: int = 1) -> None:
        categories[category] = categories.get(category, 0) + amount

    def visit(item: Any, depth: int, key: str | None = None) -> Any:
        if depth > max_depth:
            truncated["depth"] = truncated.get("depth", 0) + 1
            return "[TRUNCATED_DEPTH]"
        if key and sensitive_name(key):
            note("sensitive_field")
            return "[REDACTED]"
        if isinstance(item, str):
            clean, found, uncertain = _redact_text(item, redact_uncertain=_redact_uncertain)
            for category, count in uncertain.items():
                warnings[category] = warnings.get(category, 0) + count
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
    return Sanitized(clean, categories, sum(categories.values()), truncated, warnings)


def check_content(mapping: dict[str, Any]) -> list[dict[str, str]]:
    """Reject certain secrets; retain uncertain text and return safe warnings."""

    if not isinstance(mapping, dict):
        raise InputError("Content must be a structured mapping.")
    result = sanitize(mapping, max_text=32000, _redact_uncertain=False)
    if result.redactions:
        fields = [
            key
            for key in (
                "content",
                "source",
                "account",
                "event_id",
                "project",
                "title",
                "source_uri",
            )
            if key in mapping and mapping[key] != result.value.get(key)
        ]
        raise InputError(
            "Content rejected because it appears to contain a secret. Fields: "
            + (", ".join(fields) or "payload")
            + "."
        )
    if result.truncated:
        raise InputError("Content exceeds the supported size or structure; split the record.")
    return [{"code": "possible_secret", "category": key} for key in sorted(result.warnings)]


def guard_content(mapping: dict[str, Any]) -> dict[str, Any]:
    check_content(mapping)
    return mapping
