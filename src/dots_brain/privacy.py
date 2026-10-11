"""Secret-aware minimization for memories, audit records, and capture input."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .errors import InputError
from .privacy_text import CATEGORIES, confidence, normalized_name, scan_text
from .privacy_text import sensitive_name as _is_sensitive_name

CONTENT_WARNING_CATEGORIES = CATEGORIES
_SUMMARY_CATEGORIES = CATEGORIES
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
    warnings: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        # Preserve the stored audit/capture summary format. Warning metadata is
        # returned separately to callers that deliberately retain uncertain text.
        return {
            "categories": self.categories,
            "redactions": self.redactions,
            "truncated": self.truncated,
        }


def _redact_text(text: str):
    return scan_text(text)


def sanitize(
    value: Any,
    *,
    max_depth: int = 8,
    max_items: int = 100,
    max_text: int = 4096,
    max_nodes: int = 1000,
    max_output_bytes: int = 128 * 1024,
    _redact_uncertain: bool = True,
) -> Sanitized:
    """Return a globally bounded, JSON-safe value and non-sensitive metadata."""
    if min(max_depth, max_items, max_text, max_nodes, max_output_bytes) < 1:
        raise ValueError("Sanitizer bounds must be positive.")
    categories: dict[str, int] = {}
    warnings: dict[str, int] = {}
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
            if isinstance(item, str):
                bounded, _ = bound_for_redaction(item)
                certain = confidence(bounded, literal=True, name=normalized_name(key))
            else:
                certain = (
                    None
                    if item is None
                    else confidence(str(item), literal=True, name=normalized_name(key))
                )
            if certain is not None:
                if not certain:
                    warnings["sensitive_field"] = warnings.get("sensitive_field", 0) + 1
                if certain or _redact_uncertain:
                    note("sensitive_field")
                    return "[REDACTED]"
        if isinstance(item, str):
            bounded, was_bounded = bound_for_redaction(item)
            clean, found, uncertain = _redact_text(bounded).redact(uncertain=_redact_uncertain)
            for category, amount in uncertain.items():
                warnings[category] = warnings.get(category, 0) + amount
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
                    clean_key, found, uncertain = _redact_text(raw_key).redact(
                        uncertain=_redact_uncertain
                    )
                    for category, amount in uncertain.items():
                        warnings[category] = warnings.get(category, 0) + amount
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
    return Sanitized(clean, categories, sum(categories.values()), truncated, warnings)


def check_content(mapping: dict[str, Any]) -> list[dict[str, str]]:
    """Reject certain credentials and truncation; return safe uncertainty warnings."""
    if not isinstance(mapping, dict):
        raise InputError("Content must be a structured mapping.")
    result = sanitize(mapping, max_text=32000, _redact_uncertain=False)
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
    return [
        {"code": "possible_secret", "category": category}
        for category in sorted(result.warnings.keys() & CONTENT_WARNING_CATEGORIES)
    ]


def guard_content(mapping: dict[str, Any]) -> dict[str, Any]:
    """Compatibility wrapper; callers needing visible warnings use check_content."""
    check_content(mapping)
    return mapping
