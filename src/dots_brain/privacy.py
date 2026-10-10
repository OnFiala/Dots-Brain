"""Secret-aware minimization for memories, audit records, and capture input."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from .errors import InputError

_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9])(?:[\"']?)(?P<name>(?:[A-Za-z0-9]+[_-])*"
    r"(?:password|passwd|passphrase|pwd|heslo|secret(?:[_-](?:access[_-])?key)?|api[-_]?key|"
    r"access[-_]?token|refresh[-_]?token|token|credential))(?:[\"']?)\s*"
    r"(?P<sep>[=:])\s*(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|<[^>\r\n]*>|[^\s,;)&]+)",
    re.I,
)
_PRIVATE_KEY_HEADER = re.compile(
    r"-----BEGIN (?P<label>[^\r\n-]*"
    r"(?:PRIVATE KEY|OPENSSH PRIVATE KEY|PGP PRIVATE KEY BLOCK))-----",
    re.I,
)
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|"
    r"sk_(?:live|test|proj)_[A-Za-z0-9_-]{16,}|sk-proj-[A-Za-z0-9_-]{16,}|"
    r"sk-ant-[A-Za-z0-9_-]{16,}|"
    r"sk-[A-Za-z0-9_-]*[0-9_][A-Za-z0-9_-]{15,}|"
    r"rk_live_[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9_-]{16,}|npm_[A-Za-z0-9_-]{16,}|"
    r"AKIA[0-9A-Z]{16}|AIza[\w-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_BEARER = re.compile(r"\bBearer\s+(?=[A-Za-z0-9._~+/-]*[0-9._~+/-])[A-Za-z0-9._~+/-]{16,}", re.I)
_HEADER = re.compile(
    r"\b(?P<name>authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token|private-token)\s*:\s*(?P<value>[^\r\n]+)",
    re.I,
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
_CURL_USER = re.compile(r"(?:curl\s+)?-u\s+[^\s:]+:[^\s]+", re.I)
_BASIC = re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}", re.I)
_TELEGRAM_BOT = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_COOKIE_ASSIGNMENT = re.compile(r"\b(?:sessionid|session|cookie)\s*=\s*[^\s;'\"]+", re.I)
_PASSWORD_IS = re.compile(r"\b(?:password|passwd|pwd)\s+is\s+[^\s,;]+", re.I)
_PASSPHRASE = re.compile(r"\bpassphrase\s*:\s*[^\r\n]+", re.I)
_PLACEHOLDER = re.compile(
    r"(?:\[redacted\]|<?(?:your[ _-]?(?:token|key|secret)|"
    r"[a-z_]*(?:placeholder|example|dummy)[a-z_]*|"
    r"[a-z_]*(?:key|token|secret)_here)>?|none|null|false)",
    re.I,
)


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


def _is_sensitive_name(value: str) -> bool:
    normalized = value.lower().strip(" \t\"'`")
    if normalized in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
        return True
    parts = normalized.replace("-", "_").split("_")
    if not parts or parts[-1] in {"count", "budget", "changed", "accepted", "policy"}:
        return False
    return normalized in {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "heslo",
        "secret",
        "secret_key",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "credential",
        "session",
        "sessionid",
        "signature",
        "sig",
        "code",
    } or parts[-1] in {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "heslo",
        "secret",
        "token",
        "credential",
        "signature",
        "sig",
        "code",
    }


def _redact_private_keys(text: str, counts: dict[str, int]) -> str:
    """Redact PEM-like private keys in a forward-only pass."""
    position, pieces = 0, []
    while match := _PRIVATE_KEY_HEADER.search(text, position):
        pieces.append(text[position : match.start()])
        marker = "-----END " + match.group("label") + "-----"
        end = text.find(marker, match.end())
        pieces.append("[REDACTED_PRIVATE_KEY]")
        counts["private_key"] = counts.get("private_key", 0) + 1
        if end < 0:
            position = len(text)
            break
        position = end + len(marker)
    if not pieces:
        return text
    pieces.append(text[position:])
    return "".join(pieces)


def _redact_text(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    text = text.replace("\u200b", "")

    def replace(category: str, pattern: re.Pattern[str], replacement: str, value: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            expanded = match.expand(replacement)
            if match.group(0) != expanded:
                counts[category] = counts.get(category, 0) + 1
            return expanded

        return pattern.sub(substitute, value)

    text = _redact_private_keys(text, counts)

    def header_replace(match: re.Match[str]) -> str:
        value = match.group("value").strip()
        if not value or value in {"[REDACTED]", "[REDACTED_TOKEN]"}:
            return match.group(0)
        counts["credential_header"] = counts.get("credential_header", 0) + 1
        return f"{match.group('name')}: [REDACTED]"

    text = _HEADER.sub(header_replace, text)
    text = replace("token", _TOKEN, "[REDACTED_TOKEN]", text)
    text = replace("bearer", _BEARER, "Bearer [REDACTED]", text)
    text = replace("basic", _BASIC, "Basic [REDACTED]", text)
    text = replace("telegram_bot", _TELEGRAM_BOT, "[REDACTED_TOKEN]", text)
    text = replace("cookie_assignment", _COOKIE_ASSIGNMENT, "[REDACTED_COOKIE]", text)
    text = replace("password_assignment", _PASSWORD_IS, "password is [REDACTED]", text)
    text = replace("passphrase", _PASSPHRASE, "passphrase: [REDACTED]", text)
    text = replace("curl_credentials", _CURL_USER, "-u [REDACTED]", text)

    def assignment_replace(match: re.Match[str]) -> str:
        raw = match.group("value")
        value = raw.strip("\"'`.,")
        credential_like = (
            match.group("sep") == "="
            or raw.startswith(('"', "'"))
            or (len(value) >= 6 and any(char.isdigit() or not char.isalpha() for char in value))
        )
        if not credential_like or _PLACEHOLDER.fullmatch(value):
            return match.group(0)
        counts["assignment"] = counts.get("assignment", 0) + 1
        return f"{match.group('name')}{match.group('sep')}[REDACTED]"

    text = _ASSIGNMENT.sub(assignment_replace, text)

    def url_replace(match: re.Match[str]) -> str:
        value = match.group(0)
        try:
            parsed, changed = urlsplit(value), False
            netloc = parsed.netloc
            if parsed.password is not None:
                netloc, changed = netloc.rsplit("@", 1)[-1], True

            def scrub(raw: str) -> str:
                nonlocal changed
                pairs = parse_qsl(raw, keep_blank_values=True)
                clean = []
                for key, item in pairs:
                    if _is_sensitive_name(key):
                        clean.append((key, "[REDACTED]"))
                        changed = True
                    else:
                        clean.append((key, item))
                return "&".join(
                    f"{quote(key, safe='[]')}={quote(item, safe='[]')}" for key, item in clean
                )

            query = scrub(parsed.query)
            fragment = scrub(parsed.fragment) if "=" in parsed.fragment else parsed.fragment
            if changed:
                counts["url_credential"] = counts.get("url_credential", 0) + 1
                return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))
        except ValueError:
            counts["url_credential"] = counts.get("url_credential", 0) + 1
            return "[REDACTED_URL]"
        return value

    return _URL.sub(url_replace, text), counts


def sanitize(
    value: Any,
    *,
    max_depth: int = 8,
    max_items: int = 100,
    max_text: int = 4096,
    max_nodes: int = 1000,
    max_output_bytes: int = 128 * 1024,
) -> Sanitized:
    """Return a globally bounded, JSON-safe value and non-sensitive metadata."""
    if min(max_depth, max_items, max_text, max_nodes, max_output_bytes) < 1:
        raise ValueError("Sanitizer bounds must be positive.")
    categories: dict[str, int] = {}
    truncated: dict[str, int] = {}
    nodes = output_bytes = 0

    def note(category: str, amount: int = 1) -> None:
        categories[category] = categories.get(category, 0) + amount

    def clip(text: str, limit: int) -> str:
        nonlocal output_bytes
        try:
            text.encode("utf-8", "strict")
        except UnicodeEncodeError:
            truncated["invalid_text"] = truncated.get("invalid_text", 0) + 1
            return "[INVALID_TEXT]"
        if len(text) > limit:
            truncated["text_chars"] = truncated.get("text_chars", 0) + len(text) - limit
            text = text[:limit] + "[TRUNCATED]"
        raw, remaining = text.encode(), max_output_bytes - output_bytes
        if len(raw) > remaining:
            truncated["output_bytes"] = (
                truncated.get("output_bytes", 0) + len(raw) - max(remaining, 0)
            )
            marker = "[TRUNCATED_OUTPUT]"
            text = (
                marker
                if remaining < len(marker)
                else raw[: remaining - len(marker)].decode("utf-8", "ignore") + marker
            )
            raw = text.encode()
        output_bytes += len(raw)
        return text

    def visit(item: Any, depth: int, key: str | None = None, *, summary: bool = False) -> Any:
        nonlocal nodes, output_bytes
        nodes += 1
        if nodes > max_nodes:
            truncated["nodes"] = truncated.get("nodes", 0) + 1
            return "[TRUNCATED_NODES]"
        if depth > max_depth:
            truncated["depth"] = truncated.get("depth", 0) + 1
            return "[TRUNCATED_DEPTH]"
        if key and not summary and _is_sensitive_name(key):
            note("sensitive_field")
            return "[REDACTED]"
        if isinstance(item, str):
            clean, found = _redact_text(item)
            for category, amount in found.items():
                note(category, amount)
            return clip(clean, max_text)
        if item is None or isinstance(item, (bool, int)):
            output_bytes += 8
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                truncated["non_finite_float"] = truncated.get("non_finite_float", 0) + 1
                return "[UNSUPPORTED_NON_FINITE_FLOAT]"
            output_bytes += 24
            return item
        if isinstance(item, dict):
            output: dict[str, Any] = {}
            for index, (child_key, child) in enumerate(item.items()):
                if index >= max_items or output_bytes >= max_output_bytes:
                    name = "items" if index >= max_items else "output_bytes"
                    truncated[name] = truncated.get(name, 0) + 1
                    break
                raw_key = str(child_key)
                if raw_key == "sanitization" and isinstance(child, dict):
                    output[raw_key] = visit(child, depth + 1, summary=True)
                    continue
                clean_key, found = _redact_text(raw_key)
                for category, amount in found.items():
                    note(category, amount)
                clean_key = "[REDACTED_KEY]" if clean_key != raw_key else clean_key
                clean_key = clip(clean_key, min(max_text, 512))
                candidate, suffix = clean_key, 2
                while candidate in output:
                    candidate, suffix = f"{clean_key}#{suffix}", suffix + 1
                output[candidate] = visit(child, depth + 1, raw_key, summary=summary)
            return output
        if isinstance(item, (list, tuple)):
            output = []
            for child in item[:max_items]:
                if output_bytes >= max_output_bytes:
                    truncated["output_bytes"] = truncated.get("output_bytes", 0) + 1
                    break
                output.append(visit(child, depth + 1, summary=summary))
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
