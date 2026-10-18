"""Prepare and create a portable WB report; no collection or execution facilities."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


INPUT_LIMIT = 1024 * 1024
REPORT_LIMIT = 32 * 1024
EXCERPT_LIMIT = 4 * 1024
EXCERPTS_LIMIT = 16 * 1024
REDACTED = "[REDACTED]"
REFERENCE = Path(__file__).absolute().parent.parent / "references" / "report-template.md"

# These deliberately bounded patterns are a second pass, not universal detection.
_PRIVATE = re.compile(
    r"-----BEGIN (?P<kind>(?:[A-Z0-9]+ )*PRIVATE KEY)-----.*?"
    r"(?:-----END (?P=kind)-----|\Z)", re.DOTALL,
)
_URL = re.compile(r"(?<![a-z0-9+.-])(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/<>\"'`@]+@", re.I)
_BEARER = re.compile(r"(?P<head>\bBearer\s+)(?!\[REDACTED\])[^\s\"'`,;<>]+", re.I)
_ASSIGNMENT = re.compile(
    r"(?P<head>(?<![\w-])[\"']?-{0,2}(?:[a-z0-9]+[_-])*(?:api[-_ ]?key|(?:access|refresh|auth)[-_]?token|"
    r"token|password|passwd|pwd|client[-_]?secret|secret)[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\[REDACTED\]|[^\s,;\"'`<>\]}]+)",
    re.I,
)
_KEY = re.compile(
    r"(?<![\w-])(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})(?![\w-])"
)
_FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")


def redact(text: str) -> tuple[str, int]:
    """Retain field/category indicators while removing supported secret values."""
    count = 0

    def replace(pattern, callback):
        nonlocal text, count

        def apply(match):
            nonlocal count
            replacement = callback(match)
            count += replacement != match.group(0)
            return replacement

        text = pattern.sub(apply, text)

    replace(_PRIVATE, lambda m: f"-----BEGIN {m['kind']}-----\n{REDACTED}\n-----END {m['kind']}-----")
    replace(_URL, lambda m: m['scheme'] + REDACTED + "@")
    replace(_BEARER, lambda m: m['head'] + REDACTED)

    def assignment(match):
        value = match['value']
        quote = value[0] if value.startswith(('"', "'")) else ""
        return match['head'] + quote + REDACTED + quote

    replace(_ASSIGNMENT, assignment)
    replace(_KEY, lambda m: REDACTED)
    return text, count


def _safe_reason(error: Exception) -> str:
    # OSError.__str__ repr-escapes filenames, which can hide credential syntax
    # from the patterns. Redact the raw components before formatting them.
    if isinstance(error, OSError) and error.strerror is not None:
        parts = [redact(str(error.strerror))[0]]
        for filename in (error.filename, error.filename2):
            if filename is not None:
                parts.append("경로: " + redact(os.fsdecode(filename))[0])
        return "; ".join(parts)
    return redact(str(error))[0]


def _unindent(line: str, width: int) -> str:
    """Remove only the fence's container indentation, preserving raw content."""
    columns = 0
    index = 0
    while index < len(line) and columns < width and line[index] in " \t":
        columns += 4 - columns % 4 if line[index] == "\t" else 1
        index += 1
    return " " * max(0, columns - width) + line[index:]


def structure(markdown: str):
    """Recognize level-two headings only outside properly closed fenced blocks."""
    headings = []
    excerpts = []
    fence = None
    excerpt = []
    for number, line in enumerate(markdown.splitlines(keepends=True)):
        plain = line.rstrip("\r\n")
        match = _FENCE.match(plain)
        indent = len(match[1].expandtabs(4)) if match else None
        if fence:
            # Keep normal Markdown's zero-to-three-space closing allowance.
            # Nested containers use the opener's indentation; ambiguous changes
            # in container indentation cannot terminate the excerpt silently.
            closing_indent = indent is not None and (indent <= 3 if fence[2] <= 3 else indent == fence[2])
            if match and closing_indent and match[2][0] == fence[0] and len(match[2]) >= fence[1] and not match[3].strip():
                excerpts.append("".join(excerpt))
                fence = None
                excerpt = []
            else:
                excerpt.append(_unindent(line, fence[2]))
            continue
        if match:
            if match[2][0] == "`" and "`" in match[3]:
                raise ValueError("backtick fence의 info string이 잘못되었습니다")
            fence = (match[2][0], len(match[2]), indent)
        elif re.match(r"^ {0,3}##(?:[ \t]|$)", plain):
            headings.append((plain.rstrip(), number))
    if fence:
        raise ValueError("닫히지 않은 fenced code block이 있습니다")
    return headings, excerpts


def prepare(markdown: str) -> tuple[str, int, str | None]:
    sanitized, count = redact(markdown)
    try:
        reference_headings, _ = structure(REFERENCE.read_text(encoding="utf-8"))
        expected = [heading for heading, _ in reference_headings]
        if len(expected) != 10 or len(set(expected)) != 10:
            raise ValueError("reference에 고유한 열 가지 heading이 필요합니다")
        headings, excerpts = structure(sanitized)
        if [heading for heading, _ in headings] != expected:
            raise ValueError("reference의 열 가지 heading을 정확한 순서로 한 번씩 유지해야 합니다")
        lines = sanitized.splitlines(keepends=True)
        for index, (_, number) in enumerate(headings):
            end = headings[index + 1][1] if index + 1 < len(headings) else len(lines)
            if not "".join(lines[number + 1:end]).strip():
                raise ValueError("빈 섹션은 허용하지 않습니다. 모르는 값은 미확인으로 적으세요")
        sizes = [len(excerpt.encode("utf-8")) for excerpt in excerpts]
        if any(size > EXCERPT_LIMIT for size in sizes) or sum(sizes) > EXCERPTS_LIMIT:
            raise ValueError("원문 발췌는 각각 4 KiB, 합계 16 KiB 이내로 준비하고 누락 범위·이유를 표시하세요")
        if count:
            # The portable artifact itself must retain the redaction disclosure.
            insertion = headings[9][1]
            note = (
                f"\n가림: helper의 지원 secret 패턴 {count}곳을 {REDACTED}로 가렸습니다. "
                "지원 범주: API key/token/password 할당, Bearer, URL credential, private key. "
                "모든 임의 비밀의 탐지를 보장하지 않습니다.\n\n"
            )
            lines.insert(insertion, note)
            sanitized = "".join(lines)
        if len(sanitized.encode("utf-8")) > REPORT_LIMIT:
            raise ValueError("완성 Markdown이 UTF-8 32 KiB를 초과합니다. 생략 범위·이유를 표시해 다시 준비하세요")
        return sanitized, count, None
    except (OSError, UnicodeError, ValueError) as exc:
        return sanitized, count, _safe_reason(exc)


def _directory(path: Path) -> int:
    """Open/create each absolute directory component without following symlinks."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            created = False
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if created:
                    os.fchmod(child, 0o700)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _generated_name() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"wb-{timestamp}-{uuid.uuid4().hex}.md"


def _collision_name(target: Path, parent: int) -> str:
    """Fit a unique sibling to the destination's encoded component limit."""
    limit = os.fpathconf(parent, "PC_NAME_MAX")
    if limit < 0:  # An indeterminate/unlimited bound still needs a bounded name.
        limit = 255
    if limit == 0:
        raise OSError("출력 디렉터리의 파일명 길이 제한을 만족할 수 없습니다")
    suffix = target.suffix
    if len(os.fsencode(suffix)) + 32 > limit:
        suffix = ".md" if limit > 3 else ""
    unique = uuid.uuid4().hex[:limit - len(os.fsencode(suffix))]
    budget = limit - len(os.fsencode(suffix)) - len(unique) - 1
    stem = []
    for character in target.stem:
        size = len(os.fsencode(character))
        if size > budget:
            break
        stem.append(character)
        budget -= size
    return ("".join(stem) + "-" if stem else "") + unique + suffix


def _remove_owned(parent: int, name: str, identity: tuple[int, int]) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == identity:
            os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


def save(markdown: str, project_dir: str, output: str | None) -> Path:
    project = Path(project_dir)
    if not project.is_absolute():
        raise ValueError("--project-dir에는 확인된 절대 경로가 필요합니다")
    # Validate the project without creating a nonexistent project by accident.
    project_fd = _directory_existing(project)
    os.close(project_fd)
    target = Path(output) if output is not None else project / "bug-reports"
    if not target.is_absolute():
        target = project / target
    # Normalize dot components lexically; never resolve symlinks.
    target = Path(os.path.abspath(target))
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        target_stat = None
    if output is None or (target_stat and stat.S_ISDIR(target_stat.st_mode)):
        target = target / _generated_name()
    parent = _directory(target.parent)
    fd = None
    identity = None
    name = target.name
    try:
        for _ in range(128):
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                break
            except FileExistsError:
                name = _collision_name(target, parent)
        else:
            raise OSError("고유한 출력 파일을 만들 수 없습니다")
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        os.fchmod(fd, 0o600)
        data = markdown.encode("utf-8")
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("출력 파일의 쓰기가 진행되지 않았습니다")
            offset += written
        closing, fd = fd, None
        os.close(closing)
        return target.with_name(name)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if identity is not None:
            _remove_owned(parent, name, identity)
        raise
    finally:
        os.close(parent)


def _directory_existing(path: Path) -> int:
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--output")
    try:
        args = parser.parse_args(argv)
    except ValueError as exc:
        return _emit({"status": "invalid_draft", "reason": _safe_reason(exc)}, 2)
    try:
        raw = sys.stdin.buffer.read(INPUT_LIMIT + 1)
    except OSError as exc:
        return _emit({"status": "invalid_draft", "reason": _safe_reason(exc)}, 2)
    if len(raw) > INPUT_LIMIT:
        return _emit({"status": "invalid_draft", "reason": "입력이 1 MiB를 초과합니다. 제한에 맞춘 UTF-8 Markdown을 다시 준비하세요"}, 2)
    try:
        markdown = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _emit({"status": "invalid_draft", "reason": "입력이 올바른 UTF-8이 아닙니다", "markdown": redact(raw.decode("utf-8", errors="replace"))[0]}, 2)
    prepared, count, reason = prepare(markdown)
    if reason:
        return _emit({"status": "invalid_draft", "reason": reason, "markdown": prepared}, 2)
    try:
        path = save(prepared, args.project_dir, args.output)
    except (OSError, ValueError) as exc:
        return _emit({"status": "not_saved", "reason": _safe_reason(exc), "markdown": prepared}, 1)
    return _emit({"status": "saved", "path": redact(str(path))[0], "utf8_bytes": len(prepared.encode("utf-8")), "redaction_count": count}, 0)


def _emit(result: dict, code: int) -> int:
    # All caller-controlled strings have passed redaction before this boundary.
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
