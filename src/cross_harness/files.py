from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import tempfile


MARKER_START = "# >>> cross-harness managed >>>"
MARKER_END = "# <<< cross-harness managed <<<"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        try:
            raw = data.encode("utf-8")
        except UnicodeEncodeError:
            # Keep text artifacts writable when they contain a filesystem
            # surrogate, while never emitting an invalid UTF-8 byte sequence.
            raw = data.encode("utf-8", errors="backslashreplace")
    else:
        raw = data
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(value: object) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        rendered.encode("utf-8")
    except UnicodeEncodeError:
        # Git exposes undecodable bytes as surrogate code points. Escape only
        # those code points so ordinary non-ASCII text remains unescaped.
        rendered = "".join(
            f"\\u{ord(character):04x}"
            if 0xD800 <= ord(character) <= 0xDFFF
            else character
            for character in rendered
        )
    return rendered + "\n"


def marker_block(content: str) -> str:
    return f"{MARKER_START}\n{content.rstrip()}\n{MARKER_END}"


def append_marker(existing: str, content: str) -> str:
    if MARKER_START in existing or MARKER_END in existing:
        raise ValueError("cross-harness marker already exists")
    prefix = existing.rstrip()
    if prefix:
        prefix += "\n\n"
    return prefix + marker_block(content) + "\n"


def remove_marker(existing: str) -> str:
    start = existing.find(MARKER_START)
    end = existing.find(MARKER_END)
    if start < 0 and end < 0:
        return existing
    if start < 0 or end < start:
        raise ValueError("damaged cross-harness marker block")
    end += len(MARKER_END)
    before = existing[:start].rstrip()
    after = existing[end:].lstrip("\n")
    combined = before
    if before and after:
        combined += "\n\n"
    combined += after
    if combined and not combined.endswith("\n"):
        combined += "\n"
    return combined
