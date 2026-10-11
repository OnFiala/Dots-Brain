"""Bounded credential categories, with one forward pass per representation.

Regexes locate fixed prefixes or names of at most 64 characters. Unbounded
repetitions are possessive and do not overlap a following repetition. Values
are consumed by a cursor, including malformed quoted values, so a missing
delimiter cannot rescan the same suffix for every apparent assignment. The
fixed number of ordered match streams is merged in linear time.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit

# Keep historical labels readable in stored sanitization summaries.
CATEGORIES = frozenset(
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
_NAME = r"[A-Za-z][A-Za-z0-9_-]{0,63}"
_ASSIGNMENT = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<quote>[\"']?)(?P<name>{_NAME})(?P=quote)"
    r"[ \t]*+(?P<sep>[=:])[ \t]*+"
)
_XML = re.compile(rf"<(?P<name>{_NAME})[ \t]*+>")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<label>(?:RSA |DSA |EC |ENCRYPTED |OPENSSH )?PRIVATE KEY|"
    r"PGP PRIVATE KEY BLOCK)-----",
    re.I,
)
_PUTTY = re.compile(r"(?m)^PuTTY-User-Key-File-[0-9]{1,3}:")
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s'\"<>]++")
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}+|github_pat_[A-Za-z0-9_]{20,}+|"
    r"glpat-[A-Za-z0-9_-]{16,}+|sk_(?:live|test|proj)_[A-Za-z0-9_-]{16,}+|"
    r"sk-proj-[A-Za-z0-9_-]{16,}+|sk-ant-[A-Za-z0-9_-]{16,}+|"
    r"sk-[A-Za-z0-9_-]{16,}+|rk_live_[A-Za-z0-9_-]{16,}+|xai-[A-Za-z0-9_-]{16,}+|"
    r"hf_[A-Za-z0-9_-]{16,}+|npm_[A-Za-z0-9_-]{16,}+|GOCSPX-[A-Za-z0-9_-]{16,}+|"
    r"pypi-[A-Za-z0-9_-]{16,}+|dop_v1_[A-Za-z0-9_-]{16,}+|shpat_[A-Za-z0-9_-]{16,}+|"
    r"lin_api_[A-Za-z0-9_-]{16,}+|ntn_[A-Za-z0-9_-]{16,}+|"
    r"SG\.[A-Za-z0-9_-]{16,}+\.[A-Za-z0-9_-]{16,}+|"
    r"MTE[A-Za-z0-9_-]{17,}+\.[A-Za-z0-9_-]{5,}+\.[A-Za-z0-9_-]{20,}+|"
    r"AKIA[0-9A-Z]{16}|AIza[\w-]{20,}+|xox[baprs]-[A-Za-z0-9-]{10,}+|"
    r"eyJ[A-Za-z0-9_-]{12,512}+\.[A-Za-z0-9_-]{8,2048}+\.[A-Za-z0-9_-]{8,2048}+)"
)
_TOKEN_HEAD = re.compile(
    r"\b(?:gh[pousr]_|github_pat_|glpat-|sk_(?:live|test|proj)_|sk-|rk_live_|"
    r"xai-|hf_|npm_|GOCSPX-|pypi-|dop_v1_|shpat_|lin_api_|ntn_|SG\.|MTE|AKIA|AIza|"
    r"xox[baprs]-|eyJ)"
)
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_.-]++")
_BEARER = re.compile(r"\bBearer[ \t]++(?P<value>[A-Za-z0-9._~+/-]{16,}+)", re.I)
_BOT_TOKEN = re.compile(r"\b[0-9]{6,}+:[A-Za-z0-9_-]{20,}+\b")
_PROSE = re.compile(
    r"\b(?P<name>password|passwd|pwd|passphrase|heslo|klíč|"
    r"api[ \t]{1,8}+klíč|tajný[ \t]{1,8}+klíč|přihlašovací[ \t]{1,8}+údaje)\b",
    re.I,
)
_COMMAND = re.compile(
    r"(?:^|[;\n]|\b)(?P<command>curl|wget|sshpass|mysql|mysqldump|docker|machine)\b",
    re.I,
)
_FLAG = re.compile(rf"(?<![A-Za-z0-9_-])--(?P<name>{_NAME})(?:=|[ \t]++)")
_REFERENCE = re.compile(
    r"(?:\$[A-Za-z_][A-Za-z0-9_:]*+|\$\{[A-Za-z_][A-Za-z0-9_]*+\}|"
    r"%[A-Za-z_][A-Za-z0-9_]*+%)\Z"
)
_CODE_REFERENCE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}(?P<open>[\[(])")
_HEADERS = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "x_api_key",
        "x_auth_token",
        "private_token",
    }
)
_MARKERS = frozenset(
    {
        "[REDACTED]",
        "[REDACTED_KEY]",
        "[REDACTED_TOKEN]",
        "[REDACTED_PRIVATE_KEY]",
        "[REDACTED_URL]",
        "[REDACTED_CLI_CREDENTIAL]",
        "[TRUNCATED]",
        "[TRUNCATED_OUTPUT]",
    }
)


def normalized_name(value: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).lower().strip(" \t\"'`").replace("-", "_")


def sensitive_name(value: str) -> bool:
    name = normalized_name(value)
    if name in _HEADERS:
        return True
    parts = name.split("_")
    if parts[-1] in {"count", "budget", "changed", "accepted", "policy"}:
        return False
    if parts[-1] == "token" and parts[0] in {"page", "stop", "input", "output", "pad", "eos"}:
        return False
    return (
        name in {"apikey", "pgpassword", "sessionid"}
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
        or parts[-1] == "key"
        and any(
            part in {"api", "secret", "private", "master", "signing", "encryption", "account"}
            for part in parts[:-1]
        )
        or {"secret", "key"}.issubset(parts)
    )


def placeholder(value: str, *, code_reference: bool = False) -> bool:
    """Recognize whole placeholders, never a substring inside a credential."""
    value = value.strip().strip("\"'")
    if not value or value in _MARKERS or value in {"None", "null", "false"}:
        return True
    if _REFERENCE.fullmatch(value):
        return True
    if code_reference and (match := _CODE_REFERENCE.match(value)):
        return value.endswith(")" if match["open"] == "(" else "]")
    if value.startswith("<") and value.endswith(">"):
        return not any(char in value[1:-1] for char in "<>\r\n")
    if value.startswith("{{") and value.endswith("}}"):
        return True
    words = value.lower().replace("-", "_").replace(" ", "_").split("_")
    return (
        (
            bool(
                set(words)
                & {"your", "example", "dummy", "placeholder", "replace", "insert", "change"}
            )
            and all(
                word
                in {
                    "your",
                    "example",
                    "dummy",
                    "placeholder",
                    "api",
                    "password",
                    "passphrase",
                    "token",
                    "key",
                    "secret",
                    "credential",
                    "value",
                    "here",
                    "replace",
                    "insert",
                    "change",
                    "with",
                    "me",
                }
                for word in words
            )
        )
        or len(value) >= 3
        and set(value.lower()) <= {"x", "*"}
    )


@dataclass(frozen=True)
class Value:
    start: int
    end: int
    text: str
    quoted: bool = False
    complete: bool = True


def _value(text: str, start: int, *, line: bool = False, shell: bool = False) -> Value:
    """Consume a value once, including complete quoted and shell values."""
    for marker in _MARKERS:
        end = start + len(marker)
        if text.startswith(marker, start) and (end == len(text) or text[end] in " \t\r\n,;})]"):
            return Value(start, end, marker)
    if shell:
        # Shell quotes can be concatenated with unquoted segments. Consuming
        # just the first quoted segment would leave a credential suffix behind.
        end, quote, parts, quoted = start, "", [], False
        while end < len(text):
            char = text[end]
            if not quote and char in " \t\r\n;|&":
                break
            if char == "\\" and quote != "'" and end + 1 < len(text):
                parts.append(text[end + 1])
                end += 2
                continue
            if quote and char == quote:
                quote = ""
            elif not quote and char in "\"'":
                quote, quoted = char, True
            else:
                parts.append(char)
            end += 1
        return Value(start, end, "".join(parts), quoted, not quote)
    end = start
    if start < len(text) and text[start] in "\"'":
        quote, end = text[start], start + 1
        while end < len(text):
            if text[end] == "\\" and end + 1 < len(text):
                end += 2
            elif text[end] == quote:
                return Value(start, end + 1, text[start + 1 : end], True)
            else:
                end += 1
        return Value(start, end, text[start + 1 : end], True, False)
    if start < len(text) and text[start] == "<":
        while end < len(text) and text[end] not in ">\r\n":
            end += 1
        end += end < len(text) and text[end] == ">"
        return Value(start, end, text[start:end])
    if match := _CODE_REFERENCE.match(text, start):
        end, stack, quote = match.end(), [match["open"]], ""
        while end < len(text) and text[end] not in "\r\n":
            char = text[end]
            if quote:
                if char == "\\":
                    end = min(len(text), end + 2)
                    continue
                if char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char in "([":
                stack.append(char)
            elif char in ")]":
                if stack[-1] != ("(" if char == ")" else "["):
                    break
                stack.pop()
                if not stack:
                    return Value(start, end + 1, text[start : end + 1])
            end += 1
        return Value(start, end, text[start:end], complete=False)
    while end < len(text) and text[end] not in (",;\r\n" if line else " \t\r\n,;&)"):
        end += 1
    return Value(start, end, text[start:end])


def confidence(value: str, *, literal: bool = False, name: str = "") -> bool | None:
    """None is a reference; False is uncertain; True is a concrete credential."""
    if placeholder(value, code_reference=not literal):
        return None
    clean = value.strip()
    if not literal and clean == name:
        return None
    if name in {"token", "signature", "sig", "auth", "pass"}:
        # These labels also describe parser tokens, signatures, modes and
        # counters. Their spelling or value's punctuation is not authentication
        # context. Concrete carriers (headers, URLs, flags) supply that context.
        return False
    if literal:
        return True
    if any(char.isspace() for char in clean):
        return False
    return any(char.isdigit() or not char.isalpha() for char in clean) or (
        len(clean) >= 8 and not clean.islower()
    )


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    category: str
    certain: bool


def _credential(value: Value, category: str, *, literal: bool = False, name: str = ""):
    certain = confidence(value.text, literal=literal or value.quoted, name=name)
    if certain is not None:
        yield Match(value.start, value.end, category, certain and value.complete)


def _assignments(text: str) -> Iterator[Match]:
    cursor = 0
    while match := _ASSIGNMENT.search(text, cursor):
        cursor = match.end()
        name = normalized_name(match["name"])
        if not sensitive_name(name):
            continue
        header = name in _HEADERS
        value = _value(text, cursor, line=header or match["sep"] == ":")
        cursor = max(cursor, value.end)
        if header:
            raw = value.text.strip()
            scheme, _, payload = raw.partition(" ")
            if scheme.lower() in {"basic", "bearer", "token"}:
                if not placeholder(payload):
                    yield Match(value.start, value.end, "credential_header", True)
            else:
                yield from _credential(value, "credential_header", literal=True)
        else:
            yield from _credential(value, "assignment", name=name)


def _xml(text: str) -> Iterator[Match]:
    cursor = 0
    while match := _XML.search(text, cursor):
        cursor = match.end()
        if not sensitive_name(match["name"]):
            continue
        end = text.find("</" + match["name"] + ">", cursor)
        end = len(text) if end < 0 else end
        yield from _credential(Value(cursor, end, text[cursor:end]), "assignment", literal=True)
        cursor = end


def _pem_keys(text: str) -> Iterator[Match]:
    cursor = 0
    while match := _PRIVATE_KEY.search(text, cursor):
        marker = "-----END " + match["label"] + "-----"
        end = text.find(marker, match.end())
        cursor = len(text) if end < 0 else end + len(marker)
        yield Match(match.start(), cursor, "private_key", True)


def _keys(text: str) -> Iterator[Match]:
    putty = _PUTTY.search(text)
    putty_matches = [Match(putty.start(), len(text), "putty_private_key", True)] if putty else []
    yield from heapq.merge(_pem_keys(text), putty_matches, key=lambda item: item.start)


def _urls(text: str) -> Iterator[Match]:
    for match in _URL.finditer(text):
        try:
            parsed = urlsplit(match[0])
            values = []
            if parsed.password is not None:
                values.append((unquote(parsed.password), True, "password"))
            elif parsed.username is not None:
                values.append((unquote(parsed.username), False, "token"))
            for key, value in parse_qsl(parsed.query) + parse_qsl(parsed.fragment):
                name = normalized_name(key)
                if sensitive_name(name) or name in {"code", "session", "sessionid", "key"}:
                    # A generic key has no authentication context on its own.
                    contextual_name = (
                        "token" if name == "key" else "" if sensitive_name(name) else name
                    )
                    values.append(
                        (
                            value,
                            name not in {"code", "session", "sessionid", "key"},
                            contextual_name,
                        )
                    )
            evidence = [
                confidence(value, literal=literal, name=name) for value, literal, name in values
            ]
            if any(item is True for item in evidence):
                yield Match(match.start(), match.end(), "url_credential", True)
            elif any(item is False for item in evidence):
                yield Match(match.start(), match.end(), "url_credential", False)
        except ValueError:
            yield Match(match.start(), match.end(), "url_credential", False)


def _prose(text: str) -> Iterator[Match]:
    cursor = 0
    while match := _PROSE.search(text, cursor):
        cursor = match.end()
        # Bounded context makes the optional natural-language clause constant
        # work per head, including an arbitrary whitespace run after it.
        tail = text[cursor : cursor + 80]
        separator = re.search(r"[=:]|\b(?:is|je)\b", tail, re.I)
        if separator is None:
            continue
        prefix = tail[: separator.start()].strip()
        if prefix and prefix.split(maxsplit=1)[0].lower() not in {"k", "do", "pro"}:
            continue
        start = cursor + separator.end()
        while start < len(text) and text[start] in " \t":
            start += 1
        value = _value(text, start, line=True)
        cursor = max(start, value.end)
        yield from _credential(value, "password_assignment")


def _flags(text: str) -> Iterator[Match]:
    cursor = 0
    while match := _FLAG.search(text, cursor):
        cursor = match.end()
        if sensitive_name(match["name"]):
            value = _value(text, cursor, shell=True)
            cursor = max(cursor, value.end)
            yield from _credential(value, "cli_credentials", literal=True)


def _command_lines(text: str) -> Iterator[Match]:
    cursor = 0
    while match := _COMMAND.search(text, cursor):
        end = text.find("\n", match.end())
        end = len(text) if end < 0 else end
        cursor, position, previous = end, match.end(), ""
        command, subcommand = match["command"].lower(), ""
        while position < end:
            if text[position].isspace():
                position += 1
                continue
            if text[position] == ";":
                position += 1
                command = subcommand = previous = ""
                continue
            value = _value(text, position, shell=True)
            position = max(position + 1, value.end)
            if not command:
                command = value.text.lower()
                continue
            if command == "docker" and not subcommand:
                subcommand = value.text.lower()
            attached_user = (
                value.text.startswith("--user=")
                or value.text.startswith("-u")
                and len(value.text) > 2
            )
            if (previous in {"-u", "--user"} or attached_user) and command in {"curl", "wget"}:
                credentials = (
                    value.text.partition("=")[2]
                    if value.text.startswith("--user=")
                    else value.text[2:]
                    if attached_user
                    else value.text
                )
                if ":" in credentials:
                    _, password = credentials.split(":", 1)
                    if confidence(password, literal=True) is not None:
                        yield Match(value.start, value.end, "curl_credentials", True)
            elif previous in {"-p", "password"} and (
                command in {"sshpass", "mysql", "mysqldump", "machine"}
                or command == "docker"
                and subcommand == "login"
            ):
                yield from _credential(value, "cli_credentials", literal=True)
            elif (
                command in {"mysql", "mysqldump"}
                and value.text.startswith("-p")
                and len(value.text) > 2
            ):
                yield from _credential(
                    Value(value.start, value.end, value.text[2:]), "cli_credentials", literal=True
                )
            previous = value.text
        cursor = max(cursor, position)


def _commands(text: str) -> Iterator[Match]:
    yield from heapq.merge(_flags(text), _command_lines(text), key=lambda item: item.start)


def _credential_files(text: str) -> Iterator[Match]:
    """Two declarative credential grammars; no per-application token prefixes."""
    offset, netrc, login = 0, False, False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        words = stripped.split(maxsplit=1)
        if words and not stripped.startswith("#"):
            if words[0] in {"machine", "default"}:
                netrc, login = len(words) == 2 or words[0] == "default", False
            elif netrc and words[0] == "login":
                login = len(words) == 2
            elif netrc and login and words[0] == "password" and len(words) == 2:
                start = offset + line.index(words[1])
                yield from _credential(_value(text, start), "cli_credentials", literal=True)
            fields = stripped.split(":", 4)
            if (
                len(fields) == 5
                and all(fields[:4])
                and (
                    fields[1] == "*"
                    or fields[1].isascii()
                    and fields[1].isdigit()
                    and len(fields[1]) <= 5
                )
                and not any(char.isspace() for char in ":".join(fields[:4]))
            ):
                start = offset + line.index(stripped) + len(stripped) - len(fields[4])
                yield from _credential(
                    Value(start, start + len(fields[4]), fields[4]), "cli_credentials", literal=True
                )
        offset += len(line)


def _tokens(text: str) -> Iterator[Match]:
    def known_tokens():
        cursor = 0
        while head := _TOKEN_HEAD.search(text, cursor):
            run = _TOKEN_RUN.match(text, head.start())
            cursor = run.end()
            # A failing prefix is inspected once for its entire lexical run.
            # A regex search over the suffix would restart at every hyphen-
            # separated head and become quadratic despite possessive repeats.
            candidate = run[0].rstrip(".")
            if _TOKEN.match(candidate):
                yield Match(head.start(), head.start() + len(candidate), "token", True)

    streams = (
        known_tokens(),
        (
            Match(m.start(), m.end(), "bearer", bool(confidence(m["value"])))
            for m in _BEARER.finditer(text)
            if confidence(m["value"]) is not None
        ),
        (Match(m.start(), m.end(), "telegram_bot", True) for m in _BOT_TOKEN.finditer(text)),
    )
    yield from heapq.merge(*streams, key=lambda item: item.start)


@dataclass(frozen=True)
class TextScan:
    text: str
    matches: tuple[Match, ...]

    def redact(self, *, uncertain: bool) -> tuple[str, dict[str, int], dict[str, int]]:
        counts: dict[str, int] = {}
        warnings: dict[str, int] = {}
        pieces, cursor, end, pending_start, marker = [], 0, 0, 0, "[REDACTED]"

        def append_redaction():
            # Retain literal boundaries. Removing quotes would turn two adjacent
            # assignments into a new, larger value on the next capture pass.
            quote = self.text[pending_start]
            replacement = (
                quote + marker + quote if quote in "\"'" and self.text[end - 1] == quote else marker
            )
            pieces.extend((self.text[cursor:pending_start], replacement))

        for match in self.matches:
            if not match.certain:
                warnings[match.category] = warnings.get(match.category, 0) + 1
                if not uncertain:
                    continue
            counts[match.category] = counts.get(match.category, 0) + 1
            if end == cursor or match.start > end:
                if end > cursor:
                    append_redaction()
                    cursor = end
                pending_start, end = match.start, match.end
                marker = {"token": "[REDACTED_TOKEN]", "private_key": "[REDACTED_PRIVATE_KEY]"}.get(
                    match.category, "[REDACTED]"
                )
            else:
                end = max(end, match.end)
        if end > cursor:
            append_redaction()
            cursor = end
        pieces.append(self.text[cursor:])
        return "".join(pieces), counts, warnings


def scan_text(text: str) -> TextScan:
    text = text.replace("\u200b", "")
    urls = tuple((match.start(), match.end()) for match in _URL.finditer(text))

    def outside_urls(matches):
        position = 0
        for match in matches:
            while position < len(urls) and urls[position][1] <= match.start:
                position += 1
            if position == len(urls) or not (
                urls[position][0] <= match.start and match.end <= urls[position][1]
            ):
                yield match

    streams = (
        outside_urls(_assignments(text)),
        _xml(text),
        _keys(text),
        _urls(text),
        outside_urls(_prose(text)),
        outside_urls(_commands(text)),
        _credential_files(text),
        _tokens(text),
    )
    return TextScan(text, tuple(heapq.merge(*streams, key=lambda item: item.start)))
