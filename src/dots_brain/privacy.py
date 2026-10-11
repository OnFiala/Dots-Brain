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


@dataclass
class _Sanitizer:
    """One traversal owns its budgets, redaction counts and uncertainty metadata."""

    max_depth: int
    max_items: int
    max_text: int
    max_nodes: int
    max_output_bytes: int
    redact_uncertain: bool
    categories: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)
    truncated: dict[str, int] = field(default_factory=dict)
    nodes: int = 0
    output_bytes: int = 0

    def note(self, category: str, amount: int = 1) -> None:
        self.categories[category] = self.categories.get(category, 0) + amount

    def truncate(self, category: str, amount: int = 1) -> None:
        self.truncated[category] = self.truncated.get(category, 0) + amount

    def redact_text(self, text: str) -> str:
        clean, found, uncertain = _redact_text(text).redact(uncertain=self.redact_uncertain)
        for category, amount in uncertain.items():
            self.warnings[category] = self.warnings.get(category, 0) + amount
        for category, amount in found.items():
            self.note(category, amount)
        return clean

    def clip(self, text: str, limit: int, *, already_bounded: bool = False) -> str:
        try:
            text.encode("utf-8", "strict")
        except UnicodeEncodeError:
            self.truncate("invalid_text")
            return "[INVALID_TEXT]"
        if len(text) > limit:
            marker = "[TRUNCATED]"[:limit]
            keep = limit - len(marker)
            prefix = text[:keep]
            # A later pass must never reinterpret a clipped redaction marker.
            if "[" in prefix and prefix.rfind("[") > prefix.rfind("]"):
                prefix = prefix[: prefix.rfind("[")]
            if not already_bounded:
                self.truncate("text_chars", len(text) - len(prefix))
            text = prefix + marker
        raw, remaining = text.encode(), self.max_output_bytes - self.output_bytes
        if len(raw) > remaining:
            self.truncate("output_bytes", len(raw) - max(remaining, 0))
            marker = "[TRUNCATED_OUTPUT]"
            text = (
                marker
                if remaining < len(marker)
                else raw[: remaining - len(marker)].decode("utf-8", "ignore") + marker
            )
            raw = text.encode()
        self.output_bytes += len(raw)
        return text

    def bound_for_redaction(self, text: str) -> tuple[str, bool]:
        """Bound scans and drop a token fragment bisected by the text limit."""
        if len(text) <= self.max_text:
            return text, False
        marker = "[TRUNCATED]"[: self.max_text]
        keep = self.max_text - len(marker)
        prefix = text[:keep]
        if prefix and not prefix[-1].isspace() and not text[keep].isspace():
            start = len(prefix)
            while start and not prefix[start - 1].isspace():
                start -= 1
            prefix = prefix[:start]
            self.truncate("boundary_fragment")
        self.truncate("text_chars", len(text) - len(prefix))
        return prefix + marker, True

    def sensitive_field(self, item: Any, key: str) -> bool:
        if isinstance(item, str):
            bounded, _ = self.bound_for_redaction(item)
            certain = confidence(bounded, literal=True, name=normalized_name(key))
        else:
            certain = (
                None
                if item is None
                else confidence(str(item), literal=True, name=normalized_name(key))
            )
        if certain is None:
            return False
        if not certain:
            self.warnings["sensitive_field"] = self.warnings.get("sensitive_field", 0) + 1
        if certain or self.redact_uncertain:
            self.note("sensitive_field")
            return True
        return False

    def visit(
        self,
        item: Any,
        depth: int,
        key: str | None = None,
        *,
        summary_labels: frozenset[str] | None = None,
    ) -> Any:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            self.truncate("nodes")
            return "[TRUNCATED_NODES]"
        if depth > self.max_depth:
            self.truncate("depth")
            return "[TRUNCATED_DEPTH]"
        if summary_labels is not None:
            if isinstance(item, dict) and key in {"categories", "truncated"}:
                pass
            elif key not in summary_labels or not isinstance(item, int):
                self.note("sensitive_field")
                return "[REDACTED]"
        elif key and _is_sensitive_name(key) and self.sensitive_field(item, key):
            return "[REDACTED]"
        if isinstance(item, str):
            bounded, was_bounded = self.bound_for_redaction(item)
            return self.clip(self.redact_text(bounded), self.max_text, already_bounded=was_bounded)
        if item is None or isinstance(item, bool | int):
            self.output_bytes += 8
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                self.truncate("non_finite_float")
                return "[UNSUPPORTED_NON_FINITE_FLOAT]"
            self.output_bytes += 24
            return item
        if isinstance(item, dict):
            return self.mapping(item, depth, key, summary_labels)
        if isinstance(item, list | tuple):
            return self.sequence(item, depth)
        self.truncate("unsupported")
        return f"[UNSUPPORTED_{type(item).__name__.upper()}]"

    def mapping(
        self,
        item: dict,
        depth: int,
        key: str | None,
        summary_labels: frozenset[str] | None,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for index, (child_key, child) in enumerate(item.items()):
            if index >= self.max_items or self.output_bytes >= self.max_output_bytes:
                self.truncate("items" if index >= self.max_items else "output_bytes")
                break
            raw_key = str(child_key)
            key_limit = min(self.max_text, 512)
            if len(raw_key) > key_limit:
                # Omit the value: its key's sensitive suffix cannot be inspected.
                self.truncate("key_chars", len(raw_key))
                clean_key, child, raw_key = "[TRUNCATED_KEY]", "[REDACTED]", None
            else:
                clean_key = self.redact_text(raw_key)
                clean_key = "[REDACTED_KEY]" if clean_key != raw_key else clean_key
            clean_key = self.clip(clean_key, key_limit)
            candidate, suffix = clean_key, 2
            while candidate in output:
                candidate, suffix = f"{clean_key}#{suffix}", suffix + 1
            labels = summary_labels
            if raw_key == "categories" and key == "sanitization" and isinstance(child, dict):
                labels = _SUMMARY_CATEGORIES
            elif raw_key == "truncated" and key == "sanitization" and isinstance(child, dict):
                labels = _SUMMARY_TRUNCATIONS
            output[candidate] = self.visit(child, depth + 1, raw_key, summary_labels=labels)
        return output

    def sequence(self, item: list | tuple, depth: int) -> list[Any]:
        output = []
        for child in item[: self.max_items]:
            if self.output_bytes >= self.max_output_bytes:
                self.truncate("output_bytes")
                break
            output.append(self.visit(child, depth + 1))
        if len(item) > self.max_items:
            self.truncate("items", len(item) - self.max_items)
        return output


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
    traversal = _Sanitizer(
        max_depth,
        max_items,
        max_text,
        max_nodes,
        max_output_bytes,
        _redact_uncertain,
    )
    clean = traversal.visit(value, 0)
    return Sanitized(
        clean,
        traversal.categories,
        sum(traversal.categories.values()),
        traversal.truncated,
        traversal.warnings,
    )


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
