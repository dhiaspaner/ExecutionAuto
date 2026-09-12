"""Sanitization of anything that reaches a log, a console or a result cell.

Driver exceptions routinely echo the connection string that produced them, and
that string can carry a password. Nothing derived from an exception is shown to
a user or written to a workbook without passing through :func:`sanitize_error`.
"""

from __future__ import annotations

import re

__all__ = ["MASK", "sanitize_error", "sanitize_text", "truncate"]

MASK = "***REDACTED***"

#: Default cap for any sanitized message. Keeps result cells readable and
#: limits how much of a driver's payload can ever be echoed back.
_MAX_MESSAGE_LENGTH = 300

#: ``key=value`` pairs common to ODBC / Oracle connection strings.
_SENSITIVE_KEYS = (
    "password",
    "pwd",
    "passwd",
    "uid",
    "user id",
    "userid",
    "user",
    "username",
    "secret",
    "token",
    "apikey",
    "api key",
    "credential",
    "dsn",
    "data source",
    "server",
    "host",
    "account",
)

_KEY_VALUE_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in _SENSITIVE_KEYS) + r")\s*[=:]\s*"
    r"(\"[^\"]*\"|'[^']*'|[^;,\s}\)]*)"
)

#: JSON-ish ``"password": "value"`` pairs.
_JSON_KEY_RE = re.compile(
    r"(?i)([\"'](?:" + "|".join(re.escape(k) for k in _SENSITIVE_KEYS) + r")[\"']\s*:\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^,}\s]+)"
)

#: ``user/password@host`` as used by Oracle EasyConnect and URIs.
_URI_CREDENTIAL_RE = re.compile(r"(?i)\b([A-Za-z0-9_.$-]+)\s*/\s*([^@\s]+)\s*@")

#: A full ODBC connection string, recognised by its driver clause.
_ODBC_RE = re.compile(r"(?i)\{?\s*driver\s*=\s*\{[^}]*\}[^\n]*")


def sanitize_text(text: str, *, max_length: int = _MAX_MESSAGE_LENGTH) -> str:
    """Mask credential-bearing fragments and collapse the result to one line."""
    if not text:
        return ""
    cleaned = _ODBC_RE.sub(MASK, text)
    cleaned = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}={MASK}", cleaned)
    cleaned = _JSON_KEY_RE.sub(lambda m: f"{m.group(1)}{MASK}", cleaned)
    cleaned = _URI_CREDENTIAL_RE.sub(lambda m: f"{m.group(1)}/{MASK}@", cleaned)
    cleaned = " ".join(cleaned.split())
    return truncate(cleaned, max_length)


def sanitize_error(exc: BaseException, *, max_length: int = _MAX_MESSAGE_LENGTH) -> str:
    """Render an exception as ``Type: sanitized message``.

    The exception *type* is preserved because it is diagnostically useful and
    carries no secret; the message body is always masked and truncated.
    """
    message = sanitize_text(str(exc), max_length=max_length)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def truncate(text: str, max_length: int = _MAX_MESSAGE_LENGTH) -> str:
    """Shorten ``text`` to ``max_length`` characters with an explicit ellipsis."""
    if max_length <= 0 or len(text) <= max_length:
        return text
    return text[: max(0, max_length - 3)].rstrip() + "..."
