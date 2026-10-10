"""Secret-aware minimization for memories, audit records, and capture input."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from .errors import InputError

# Scan assignment-shaped text with bounded names.  The previous expression tried
# every possible underscore-delimited prefix before it could reject it.
_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<quote>[\"']?)(?P<name>[A-Za-z][A-Za-z0-9_-]{0,63})"
    r"(?P=quote)[ \t]*(?P<sep>[=:])[ \t]*"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|<[^>\r\n]*>|[^\s,;)&]+)",
    re.I,
)
_PRIVATE_KEY_HEADER = re.compile(
    r"(?:-----)?BEGIN (?P<label>(?:RSA |DSA |EC |ENCRYPTED )?PRIVATE KEY|"
    r"OPENSSH PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----",
    re.I,
)
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|"
    r"sk_(?:live|test|proj)_[A-Za-z0-9_-]{16,}|sk-proj-[A-Za-z0-9_-]{16,}|"
    r"sk-ant-[A-Za-z0-9_-]{16,}|"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"rk_live_[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9_-]{16,}|npm_[A-Za-z0-9_-]{16,}|"
    r"GOCSPX-[A-Za-z0-9_-]{16,}|pypi-[A-Za-z0-9_-]{16,}|dop_v1_[A-Za-z0-9_-]{16,}|"
    r"shpat_[A-Za-z0-9_-]{16,}|lin_api_[A-Za-z0-9_-]{16,}|ntn_[A-Za-z0-9_-]{16,}|"
    r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|"
    r"MTE[A-Za-z0-9_-]{17,}+\.[A-Za-z0-9_-]{5,}+\.[A-Za-z0-9_-]{20,}+|"
    r"AKIA[0-9A-Z]{16}|AIza[\w-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{12,512}+\.[A-Za-z0-9_-]{8,2048}+\.[A-Za-z0-9_-]{8,2048}+)\b"
)
_BEARER = re.compile(r"\bBearer\s+(?=[A-Za-z0-9._~+/-]*[0-9._~+/-])[A-Za-z0-9._~+/-]{16,}", re.I)
_HEADER = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<quote>[\"']?)(?P<name>authorization|proxy-authorization|cookie|"
    r"set-cookie|x-api-key|x-auth-token|private-token)(?P=quote)\s*:\s*(?P<value>[^\r\n]+)",
    re.I,
)
_URL = re.compile(
    r"\b(?:https?|ftp|postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?|mysql|mongodb(?:\+[A-Za-z0-9_]+)?|"
    r"redis|amqps?|sftp)://[^\s'\"<>]+",
    re.I,
)
_CURL_USER = re.compile(r"\bcurl\s+(?:-u|--user)\s+[^\s:]+:[^\s]+", re.I)
_CLI_PASSWORD = re.compile(
    r"\b(?:wget\s+--user=[^\s]+\s+--password=|mysql\s+-u\s+[^\s]+\s+-p|"
    r"sshpass\s+-p\s+|mysqldump\s+--user=[^\s]+\s+--password\s+|"
    r"docker\s+login\s+[^\s]+\s+-u\s+[^\s]+\s+-p\s+)(?:'[^'\r\n]*'|[^\s\r\n]+)",
    re.I,
)
_CZECH_SECRET = re.compile(
    r"\b(?:nové\s+)?(?:heslo|klíč|api\s+klíč|tajný\s+klíč|přihlašovací\s+údaje)"
    r"(?:[ \t]+(?:k|do|pro)[ \t]+[^:\r\n]{1,64})?[ \t]*(?::|=|je)[ \t]*"
    r"(?P<value>'[^'\r\n]*'|\"[^\"\r\n]*\"|[^\s,;\r\n]+)",
    re.I,
)
_CZECH_LOGIN = re.compile(r"\bpřihlašovací\s+údaje\s*:\s*[^\s/\r\n]+\s*/\s*[^\s/\r\n]+", re.I)
_PUTTY_PRIVATE_KEY = re.compile(r"(?m)^PuTTY-User-Key-File-[^\r\n]*(?:\r?\n[^\r\n]*)*")
_TELEGRAM_BOT = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_PASSWORD_IS = re.compile(r"\b(?P<name>password|passwd|pwd)\s+is\s+(?P<value>[^\s,;]+)", re.I)
_PASSPHRASE = re.compile(r"\bpassphrase\s*:\s*[^\r\n]+", re.I)
_PLACEHOLDER = re.compile(
    r"(?:\[redacted\]|<?(?:your[ _-]?(?:token|key|secret)|"
    r"[a-z_]*(?:placeholder|example|dummy)[a-z_]*|"
    r"[a-z_]*(?:key|token|secret)_here)>?|none|null|false)",
    re.I,
)
_DIAGNOSTIC_FIELDS = frozenset({"status_code", "error_code", "exit_code", "code", "session"})
_DIAGNOSTIC_VALUE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_SUMMARY_CATEGORIES = frozenset(
    {
        "private_key",
        "putty_private_key",
        "credential_header",
        "token",
        "bearer",
        "basic",
        "telegram_bot",
        "cookie_assignment",
        "password_assignment",
        "passphrase",
        "curl_credentials",
        "cli_credentials",
        "czech_assignment",
        "assignment",
        "url_credential",
        "sensitive_field",
    }
)
_SUMMARY_TRUNCATIONS = frozenset(
    {
        "invalid_text",
        "text_chars",
        "output_bytes",
        "boundary_fragment",
        "nodes",
        "depth",
        "key_chars",
        "items",
        "non_finite_float",
        "unsupported",
    }
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


def _normalized_field_name(value: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).lower().strip(" \t\"'`")


def _is_sensitive_name(value: str) -> bool:
    normalized = _normalized_field_name(value)
    if normalized in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
        return True
    parts = normalized.replace("-", "_").split("_")
    if not parts or parts[-1] in {"count", "budget", "changed", "accepted", "policy"}:
        return False
    if parts[-1] == "token" and parts[0] in {"page", "stop", "input", "output", "pad", "eos"}:
        return False
    return (
        normalized
        in {
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
            "pass",
            "auth",
            "db_pass",
            "pgpassword",
            "signature",
            "sig",
            "sessionid",
        }
        or parts[-1]
        in {
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
            "pass",
            "auth",
        }
        or (
            parts[-1] == "key"
            and any(
                part in {"api", "secret", "signing", "encryption", "account"} for part in parts[:-1]
            )
        )
        or ({"secret", "key"}.issubset(parts))
    )


def _is_diagnostic_value(value: Any) -> bool:
    return isinstance(value, int) or (
        isinstance(value, str) and _DIAGNOSTIC_VALUE.fullmatch(value) is not None
    )


def _is_sensitive_query_name(value: str) -> bool:
    """Query names are credentials by convention; mapping fields are not."""
    return _is_sensitive_name(value) or _normalized_field_name(value) in {
        "code",
        "session",
        "sessionid",
    }


def _is_credential_value(value: str) -> bool:
    """Keep prose such as ``password is stored`` out of the secret path."""
    cleaned = value.strip("\"'`.,;:!?)}")
    return (
        bool(cleaned)
        and not (
            cleaned.startswith(("[REDACTED]", "[TRUNCATED]")) or cleaned.endswith("[TRUNCATED]")
        )
        and not any(char.isspace() for char in cleaned)
        and not _PLACEHOLDER.fullmatch(cleaned)
        and (
            value.startswith(('"', "'", "<"))
            or len(cleaned) >= 8
            and any(char.isdigit() or not char.isalpha() for char in cleaned)
        )
    )


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
    text = replace("putty_private_key", _PUTTY_PRIVATE_KEY, "[REDACTED_PRIVATE_KEY]", text)

    def header_replace(match: re.Match[str]) -> str:
        value = match.group("value").strip()
        scheme = value.lstrip("\"'").lower().startswith(("basic ", "bearer ", "token "))
        if (
            not value
            or value.startswith(("[REDACTED]", "[TRUNCATED]"))
            or not scheme
            and any(char.isspace() for char in value)
        ):
            return match.group(0)
        counts["credential_header"] = counts.get("credential_header", 0) + 1
        quote = match.group("quote")
        return f"{quote}{match.group('name')}{quote}: [REDACTED]"

    text = _HEADER.sub(header_replace, text)
    text = replace("token", _TOKEN, "[REDACTED_TOKEN]", text)
    text = replace("bearer", _BEARER, "Bearer [REDACTED]", text)
    text = replace("telegram_bot", _TELEGRAM_BOT, "[REDACTED_TOKEN]", text)

    def password_is_replace(match: re.Match[str]) -> str:
        if not _is_credential_value(match.group("value")):
            return match.group(0)
        counts["password_assignment"] = counts.get("password_assignment", 0) + 1
        return f"{match.group('name')} is [REDACTED]"

    text = _PASSWORD_IS.sub(password_is_replace, text)

    def passphrase_replace(match: re.Match[str]) -> str:
        value = match.group(0).split(":", 1)[1].strip()
        if not _is_credential_value(value):
            return match.group(0)
        counts["passphrase"] = counts.get("passphrase", 0) + 1
        return "passphrase: [REDACTED]"

    text = _PASSPHRASE.sub(passphrase_replace, text)

    def curl_replace(match: re.Match[str]) -> str:
        if "$" in match.group(0):
            return match.group(0)
        counts["curl_credentials"] = counts.get("curl_credentials", 0) + 1
        return "-u [REDACTED]"

    text = _CURL_USER.sub(curl_replace, text)
    text = replace("cli_credentials", _CLI_PASSWORD, "[REDACTED_CLI_CREDENTIAL]", text)

    def czech_assignment_replace(match: re.Match[str]) -> str:
        if not _is_credential_value(match.group("value")):
            return match.group(0)
        counts["czech_assignment"] = counts.get("czech_assignment", 0) + 1
        return "[REDACTED]"

    text = _CZECH_SECRET.sub(czech_assignment_replace, text)
    text = replace("czech_assignment", _CZECH_LOGIN, "[REDACTED]", text)

    def assignment_replace(match: re.Match[str]) -> str:
        if not _is_sensitive_name(match.group("name")):
            return match.group(0)
        raw = match.group("value")
        value = raw.strip("\"'`.,")
        if match.start() > 0 and text[match.start() - 1] == "." and raw == match.group("name"):
            # Constructor assignment from the same named variable is code,
            # while self.password = "literal value" still needs redaction.
            return match.group(0)
        if _PLACEHOLDER.fullmatch(value):
            return match.group(0)
        if _normalized_field_name(match.group("name")) in {
            "session",
            "sessionid",
            "cookie",
        } and not _is_credential_value(raw):
            return match.group(0)
        normalized_name = _normalized_field_name(match.group("name"))
        if any(marker in raw for marker in ("(", "[", "]")) or raw.startswith(("os.", "getpass.")):
            return match.group(0)
        line_ends = match.end() == len(text) or text[match.end()] in "\r\n"
        plain_config_value = (
            normalized_name in {"password", "passwd", "pass", "secret", "token"}
            and line_ends
            and len(value) >= 8
        )
        if normalized_name == "token" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return match.group(0)
        if match.group("sep") == ":" and not (_is_credential_value(raw) or plain_config_value):
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
            if parsed.hostname in {
                "hooks.slack.com",
                "discord.com",
                "hooks.zapier.com",
                "api.telegram.org",
            }:
                counts["url_credential"] = counts.get("url_credential", 0) + 1
                return "[REDACTED_URL]"

            def scrub(raw: str) -> str:
                nonlocal changed
                pairs = parse_qsl(raw, keep_blank_values=True)
                clean = []
                for key, item in pairs:
                    if (
                        _is_sensitive_query_name(key)
                        and not item.startswith(("[REDACTED]", "[TRUNCATED]"))
                        and (_normalized_field_name(key) != "code" or _is_credential_value(item))
                    ):
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

    def clip(text: str, limit: int, *, already_bounded: bool = False) -> str:
        nonlocal output_bytes
        try:
            text.encode("utf-8", "strict")
        except UnicodeEncodeError:
            truncated["invalid_text"] = truncated.get("invalid_text", 0) + 1
            return "[INVALID_TEXT]"
        if len(text) > limit:
            marker = "[TRUNCATED]"[:limit]
            keep = limit - len(marker)
            prefix = text[:keep]
            # A later pass must never reinterpret a clipped redaction marker.
            if "[" in prefix and prefix.rfind("[") > prefix.rfind("]"):
                prefix = prefix[: prefix.rfind("[")]
            if not already_bounded:
                truncated["text_chars"] = truncated.get("text_chars", 0) + len(text) - len(prefix)
            text = prefix + marker
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

    def bound_for_redaction(text: str) -> tuple[str, bool]:
        """Never run credential regexes over an unbounded payload.

        The final non-whitespace fragment is dropped when the boundary bisects a
        token-like value.  This prevents retaining a prefix of a credential that
        extends past the collection limit.  A strict caller rejects the
        resulting truncation, while capture callers retain an explicit marker.
        """
        if len(text) <= max_text:
            return text, False
        marker = "[TRUNCATED]"[:max_text]
        keep = max_text - len(marker)
        prefix = text[:keep]
        if prefix and not prefix[-1].isspace() and not text[keep].isspace():
            start = len(prefix)
            while start and not prefix[start - 1].isspace():
                start -= 1
            prefix = prefix[:start]
            truncated["boundary_fragment"] = truncated.get("boundary_fragment", 0) + 1
        truncated["text_chars"] = truncated.get("text_chars", 0) + len(text) - len(prefix)
        return prefix + marker, True

    def visit(
        item: Any,
        depth: int,
        key: str | None = None,
        *,
        summary_labels: frozenset[str] | None = None,
    ) -> Any:
        nonlocal nodes, output_bytes
        nodes += 1
        if nodes > max_nodes:
            truncated["nodes"] = truncated.get("nodes", 0) + 1
            return "[TRUNCATED_NODES]"
        if depth > max_depth:
            truncated["depth"] = truncated.get("depth", 0) + 1
            return "[TRUNCATED_DEPTH]"
        if summary_labels is not None:
            if isinstance(item, dict) and key in {"categories", "truncated"}:
                pass
            elif key not in summary_labels or not isinstance(item, int):
                note("sensitive_field")
                return "[REDACTED]"
        elif key and _is_sensitive_name(key):
            if isinstance(item, str) and item in {
                "[REDACTED]",
                "[REDACTED_KEY]",
                "[REDACTED_TOKEN]",
                "[REDACTED_PRIVATE_KEY]",
                "[REDACTED_URL]",
                "[REDACTED_CLI_CREDENTIAL]",
            }:
                return item
            note("sensitive_field")
            return "[REDACTED]"
        elif _normalized_field_name(key or "") in _DIAGNOSTIC_FIELDS and not _is_diagnostic_value(
            item
        ):
            note("sensitive_field")
            return "[REDACTED]"
        if isinstance(item, str):
            bounded, was_bounded = bound_for_redaction(item)
            clean, found = _redact_text(bounded)
            for category, amount in found.items():
                note(category, amount)
            return clip(clean, max_text, already_bounded=was_bounded)
        if item is None or isinstance(item, bool | int):
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
                key_limit = min(max_text, 512)
                if len(raw_key) > key_limit:
                    # Its sensitive suffix cannot be inspected within the budget.
                    # Omit both the key and its value rather than trust a prefix.
                    truncated["key_chars"] = truncated.get("key_chars", 0) + len(raw_key)
                    clean_key, child, raw_key = "[TRUNCATED_KEY]", "[REDACTED]", None
                else:
                    clean_key, found = _redact_text(raw_key)
                    for category, amount in found.items():
                        note(category, amount)
                    clean_key = "[REDACTED_KEY]" if clean_key != raw_key else clean_key
                clean_key = clip(clean_key, key_limit)
                candidate, suffix = clean_key, 2
                while candidate in output:
                    candidate, suffix = f"{clean_key}#{suffix}", suffix + 1
                labels = summary_labels
                if raw_key == "categories" and key == "sanitization" and isinstance(child, dict):
                    labels = _SUMMARY_CATEGORIES
                elif raw_key == "truncated" and key == "sanitization" and isinstance(child, dict):
                    labels = _SUMMARY_TRUNCATIONS
                output[candidate] = visit(child, depth + 1, raw_key, summary_labels=labels)
            return output
        if isinstance(item, list | tuple):
            output = []
            for child in item[:max_items]:
                if output_bytes >= max_output_bytes:
                    truncated["output_bytes"] = truncated.get("output_bytes", 0) + 1
                    break
                output.append(visit(child, depth + 1))
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
        fields = [
            field
            for field in (
                "content",
                "source",
                "account",
                "event_id",
                "project",
                "title",
                "source_uri",
            )
            if field in mapping and mapping[field] != result.value.get(field)
        ]
        categories = sorted(result.categories.keys() & _SUMMARY_CATEGORIES)
        raise InputError(
            "Content rejected because it appears to contain a secret. "
            f"Fields: {', '.join(fields) or 'payload'}; "
            f"categories: {', '.join(categories) or 'credential'}."
        )
    if result.truncated:
        raise InputError("Content exceeds the supported size or structure; split the record.")
    return result.value
