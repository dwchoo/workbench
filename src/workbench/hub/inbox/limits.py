"""Inbox rejection type and input limits (SPEC "입력 한도", D-36).

Every check runs before anything is written; a rejected input leaves the inbox file bytes
unchanged.
"""
from __future__ import annotations

import re
import secrets
import unicodedata


def confirm_token() -> str:
    """A 24-byte URL-safe token a person (or an agent) types after ``--confirm``. A leading ``-`` would read as an
    option, so a draw that starts with one is discarded and drawn again (same length, charset and entropy
    minus the 1/64 of draws that are dropped)."""
    while True:
        token = secrets.token_urlsafe(24)
        if not token.startswith("-"):
            return token


class InboxError(Exception):
    """A refused inbox operation: a reason ``code`` plus the request's current state/version.

    ``state``/``version`` are only filled when the caller may see the request, so a
    ``not_found`` never reveals whether a hidden request exists.
    """

    def __init__(self, code: str, detail: str = "", *, state: str | None = None,
                 version: int | None = None):
        self.code, self.detail, self.state, self.version = code, detail, state, version
        super().__init__(f"{code}: {detail}" if detail else code)

    def to_json(self) -> dict:
        value: dict = {"code": self.code, "detail": self.detail}
        if self.state is not None:
            value["state"] = self.state
        if self.version is not None:
            value["version"] = self.version
        return value


TITLE_MAX_CHARS = 200
BODY_MAX_BYTES = 64 * 1024
COMMENT_MAX_BYTES = 16 * 1024
SUMMARY_MAX_BYTES = 16 * 1024
QUERY_MAX_CHARS = 200
LIST_LIMIT_MAX = 100
LIST_LIMIT_DEFAULT = 50
_TITLE_FORBIDDEN = frozenset({"Cc", "Cf", "Zl", "Zp"})
_TITLE_ALLOWED_FORMAT = frozenset({"\u200d"})  # ZERO WIDTH JOINER: emoji sequences (a family, a flag variant)
_IDEMPOTENCY_KEY =re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _utf8_size(value: object, what: str) -> int:
    if not isinstance(value, str):
        raise InboxError("invalid_input", f"{what} must be a string")
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise InboxError("invalid_input", f"{what} is not valid Unicode text") from exc


def title(value: object) -> str:
    _utf8_size(value, "title")
    assert isinstance(value, str)
    if not value:
        raise InboxError("invalid_input", "title is empty")
    if len(value) > TITLE_MAX_CHARS:
        raise InboxError("too_large", f"title exceeds {TITLE_MAX_CHARS} characters")
    # Cc controls, Cf format (bidi overrides, zero-width; the emoji joiner aside), Zl/Zp line and paragraph
    # separators.
    if any(unicodedata.category(ch) in _TITLE_FORBIDDEN and ch not in _TITLE_ALLOWED_FORMAT for ch in value):
        raise InboxError("invalid_input", "title contains control, format or separator characters")
    return value


def _sized_text(value: object, what: str, maximum: int) -> str:
    size = _utf8_size(value, what)
    if size == 0:
        raise InboxError("invalid_input", f"{what} is empty")
    if size > maximum:
        raise InboxError("too_large", f"{what} exceeds {maximum} bytes")
    assert isinstance(value, str)
    return value


def body(value: object) -> str:
    return _sized_text(value, "body", BODY_MAX_BYTES)


def comment(value: object) -> str:
    return _sized_text(value, "comment", COMMENT_MAX_BYTES)


def summary(value: object) -> str:
    return _sized_text(value, "summary", SUMMARY_MAX_BYTES)


def idempotency_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise InboxError("invalid_input", "idempotency_key must be 1-128 characters of [A-Za-z0-9._:-]")
    return value


def query(value: object) -> str | None:
    if value is None:
        return None
    _utf8_size(value, "query")
    assert isinstance(value, str)
    if len(value) > QUERY_MAX_CHARS:
        raise InboxError("invalid_input", f"query exceeds {QUERY_MAX_CHARS} characters")
    return value


def search_key(text: str) -> str:
    """``text`` for a caseless, canonical-equivalence substring match (Unicode D145: NFD of the case fold of
    NFD), so a composed Hangul query finds decomposed text (macOS file names) and the other way round."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())


def list_limit(value: object) -> int:
    if value is None:
        return LIST_LIMIT_DEFAULT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= LIST_LIMIT_MAX:
        raise InboxError("invalid_input", f"limit must be 1-{LIST_LIMIT_MAX}")
    return value
