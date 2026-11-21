"""Read changes against the pinned source without touching the checkout."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_QUOTED_NAME = re.compile(r'"(?:\\.|[^"\\])*"')
_GIT_ESCAPE = re.compile(r"\\(?:[0-7]{3}|.)")
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
_NO_NEWLINE = r"\ No newline at end of file"


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[tuple[str, str], ...]


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    old_lines: set[int] = field(default_factory=set)
    insertion_anchors: set[int] = field(default_factory=set)
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False


def _lines(text: str) -> list[str]:
    """Split on LF only. A CR or Unicode line separator is data."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _path(raw: str, prefix: str = "") -> str | None:
    if raw.startswith('"'):
        match = _QUOTED_NAME.match(raw)
        if match is None or raw[match.end():].partition("\t")[0].strip():
            raise ValueError(f"invalid quoted diff path: {raw!r}")
        body = match.group()[1:-1]
        encoded = bytearray()
        offset = 0
        for escape in _GIT_ESCAPE.finditer(body):
            encoded.extend(body[offset:escape.start()].encode("utf-8", "surrogateescape"))
            value = escape.group()[1:]
            if len(value) == 3:
                number = int(value, 8)
                if number > 255:
                    raise ValueError(f"invalid Git path escape: {value!r}")
                encoded.append(number)
            elif value in _ESCAPES:
                encoded.append(_ESCAPES[value])
            else:
                raise ValueError(f"unsupported Git path escape: {value!r}")
            offset = escape.end()
        encoded.extend(body[offset:].encode("utf-8", "surrogateescape"))
        name = encoded.decode("utf-8", "surrogateescape")
    else:
        name = raw.partition("\t")[0]
    if name == "/dev/null":
        return None
    if prefix and name.startswith(prefix):
        name = name[len(prefix):]
    path = PurePosixPath(name)
    if not name or name == "." or "\0" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid diff path: {name!r}")
    return name


def _git_paths(header: str) -> tuple[str | None, str | None]:
    if header.startswith('"'):
        match = _QUOTED_NAME.match(header)
        if match is None or not header[match.end():].startswith(" "):
            raise ValueError(f"invalid diff --git header: {header!r}")
        return _path(match.group(), "a/"), _path(header[match.end() + 1:], "b/")

    # File names with spaces need not be quoted by Git. Prefer the split that
    # yields identical old and new names, then let the file headers override it.
    separators = tuple(re.finditer(r' (?=b/|"b/)', header))
    if not separators:
        parts = header.split(" ", 1)
        if len(parts) != 2:
            raise ValueError(f"invalid diff --git header: {header!r}")
        return _path(parts[0], "a/"), _path(parts[1], "b/")
    choices = [
        (_path(header[:separator.start()], "a/"), _path(header[separator.end():], "b/"))
        for separator in separators
    ]
    return next((paths for paths in choices if paths[0] == paths[1]), choices[0])


def _read_hunk(lines: list[str], index: int) -> tuple[Hunk, int]:
    match = _HUNK_HEADER.fullmatch(lines[index].rstrip("\r\n"))
    if match is None:
        raise ValueError(f"invalid hunk header at patch line {index + 1}")
    old_start, old_count, new_start, new_count = (
        int(value) if value is not None else 1 for value in match.groups()
    )
    if (old_count and not old_start) or (new_count and not new_start) or not (old_count or new_count):
        raise ValueError(f"invalid hunk range at patch line {index + 1}")

    index += 1
    body: list[tuple[str, str]] = []
    old_read = new_read = 0
    while index < len(lines):
        line = lines[index]
        if line.rstrip("\r\n") == _NO_NEWLINE:
            if not body or not body[-1][1].endswith("\n"):
                raise ValueError(f"misplaced no-newline marker at patch line {index + 1}")
            kind, text = body[-1]
            body[-1] = kind, text[:-1]
        elif old_read == old_count and new_read == new_count:
            break
        else:
            kind = line[0]
            if kind not in " +-":
                raise ValueError(f"expected hunk content at patch line {index + 1}")
            old_read += kind != "+"
            new_read += kind != "-"
            if old_read > old_count or new_read > new_count:
                raise ValueError(f"hunk exceeds its declared ranges at patch line {index + 1}")
            contents = line[1:]
            if not contents.endswith("\n"):
                contents += "\n"
            body.append((kind, contents))
        index += 1
    if (old_read, new_read) != (old_count, new_count):
        raise ValueError(f"truncated hunk at patch line {index + 1}")
    return Hunk(old_start, old_count, new_start, new_count, tuple(body)), index


def _old_offset(hunk: Hunk) -> int:
    return hunk.old_start - 1 if hunk.old_count else hunk.old_start


def _add_hunk(changes: FileDiff, hunk: Hunk) -> None:
    cursor = _old_offset(hunk)
    removed_in_block = added_in_block = False
    for kind, _ in hunk.lines:
        if kind == "+":
            added_in_block = True
        elif kind == "-":
            removed_in_block = True
            cursor += 1
            changes.old_lines.add(cursor)
        else:
            if added_in_block and not removed_in_block:
                changes.insertion_anchors.add(cursor)
            removed_in_block = added_in_block = False
            cursor += 1
    if added_in_block and not removed_in_block:
        changes.insertion_anchors.add(cursor)
    changes.hunks.append(hunk)


def parse_patch(text: str) -> list[FileDiff]:
    """Return changed files, old changed lines and pure-insertion anchors.

    An anchor is the line before an insertion in the old file, with 0 for an
    insertion at the start. Malformed hunks raise ValueError.
    """
    lines = _lines(text)
    files: list[FileDiff] = []
    current: FileDiff | None = None
    has_file_header = False
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        if line.startswith("diff --git "):
            current = FileDiff(*_git_paths(line[11:]))
            files.append(current)
            has_file_header = False
        elif line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old_path = _path(line[4:], "a/")
            new_path = _path(lines[index + 1].rstrip("\r\n")[4:], "b/")
            if current is None or has_file_header:
                current = FileDiff(old_path, new_path)
                files.append(current)
            else:
                current.old_path, current.new_path = old_path, new_path
            has_file_header = True
            index += 1
        elif line.startswith("@@"):
            if current is None or not has_file_header or current.binary:
                raise ValueError(f"hunk has no text file header at patch line {index + 1}")
            hunk, index = _read_hunk(lines, index)
            _add_hunk(current, hunk)
            continue
        elif current is not None and line.startswith("new file mode "):
            current.old_path = None
        elif current is not None and line.startswith("deleted file mode "):
            current.new_path = None
        elif current is not None and line.startswith(("rename from ", "copy from ")):
            current.old_path = _path(line.split(" ", 2)[2])
        elif current is not None and line.startswith(("rename to ", "copy to ")):
            current.new_path = _path(line.split(" ", 2)[2])
        elif current is not None and (line == "GIT binary patch" or line.startswith("Binary files ")):
            current.binary = True
        elif current is not None and current.hunks and line and line[0] in " +-\\" and line != "-- ":
            raise ValueError(f"unexpected content after hunk at patch line {index + 1}")
        index += 1
    return files


def apply_to_text(original: str, file_diff: FileDiff) -> str:
    """Apply a file's hunks at their exact coordinates using the given text."""
    if file_diff.binary:
        raise ValueError("cannot apply a binary patch to text")
    if file_diff.old_path is None and original:
        raise ValueError("a newly added file requires empty original text")

    source = _lines(original)
    result: list[str] = []
    cursor = 0
    for hunk in file_diff.hunks:
        start = _old_offset(hunk)
        end = start + hunk.old_count
        new_start = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        if start < cursor or end > len(source):
            raise ValueError(f"old hunk range at line {hunk.old_start} is outside the remaining file")
        result.extend(source[cursor:start])
        if new_start != len(result):
            raise ValueError(f"new hunk range at line {hunk.new_start} disagrees with earlier hunks")
        cursor = start
        for kind, contents in hunk.lines:
            if kind != "+":
                if cursor >= end or source[cursor] != contents:
                    raise ValueError(f"patch context does not match original line {cursor + 1}")
                cursor += 1
            if kind != "-":
                result.append(contents)
    result.extend(source[cursor:])
    if file_diff.new_path is None and any(result):
        raise ValueError("a deleted file must have no remaining text")
    if any(not line.endswith("\n") for line in result[:-1]):
        raise ValueError("a no-newline marker appears before the end of the new file")
    return "".join(result)
