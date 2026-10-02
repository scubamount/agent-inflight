"""Write safety for the tracker: forged-entry rejection, credential detection,
private file modes, and an advisory lock.

Every writer (`add`, `done`, `trim`, the Hermes plugin's retag) goes through
`locked()` and `write_private()`, so the rules hold no matter who writes.
"""
from __future__ import annotations

import contextlib
import errno
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Iterator, List

from . import core

FILE_MODE = 0o600
DIR_MODE = 0o700
LOCK_TIMEOUT_S = 5.0

# Credential shapes. Matches report the KIND only; the value is never echoed.
SECRET_PATTERNS = [
    # Real keys end in a long random run (sk-ant-api03-<run>, sk-proj-<run>,
    # sk-<48>). Require a 16+ alnum run holding a letter AND a digit, so
    # hyphenated words (sk-learn-pipeline, sk-integration-tests) don't match.
    ("openai/anthropic-style key", re.compile(
        r"\bsk-(?:[A-Za-z0-9]+[-_]){0,3}(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{16,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("URL with embedded password", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@[^\s/]+", re.I)),
    ("password/token assignment", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
]


def secret_kinds(text: str) -> List[str]:
    """Kinds of credential-looking strings in `text` (deduped, stable order)."""
    seen: List[str] = []
    for kind, rx in SECRET_PATTERNS:
        if rx.search(text) and kind not in seen:
            seen.append(kind)
    return seen


def headline_problems(headline: str) -> List[str]:
    """Why a headline can't be written as-is. A headline is one line of plain
    text; anything that could start a second entry or spoof a tag is refused."""
    out: List[str] = []
    if "\n" in headline or "\r" in headline:
        out.append("headline contains a newline")
    if core.TAG_RE.search(headline) or core.ID_ONLY_RE.search(headline) or core.LITERAL_TAG_RE.search(headline):
        out.append("headline contains a session/entry tag; `add` writes the tag itself")
    if re.search(r"(?m)^##\s", headline):
        out.append("headline starts a section header")
    return out


def body_problems(body: str) -> List[str]:
    """Body may be multi-line, but no line may start a new entry or section:
    that would forge an entry (possibly tagged as another session)."""
    out: List[str] = []
    for line in body.splitlines():
        if core.ENTRY_DATE_HEAD.match(line):
            out.append("body line starts a new dated entry (`**YYYY-MM-DD`)")
            break
    if re.search(r"(?m)^## ", body):
        out.append("body line starts a section header (`## `)")
    return out


def ensure_private_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, DIR_MODE)


def write_private(path: Path, text: str) -> None:
    """Atomic replace; the result is always mode 0600 (mkstemp creates 0600)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def create_private(path: Path, text: str) -> None:
    """Create a new file 0600; fails if it exists (no clobber)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def locked(path: Path, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Exclusive advisory lock on `<path>.lock` for the read-modify-write.
    Every writer takes it, so a write landing between another writer's
    check and replace can no longer be lost. POSIX only (fcntl); on a
    platform without fcntl the lock is a no-op and the stat check remains."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    lock = path.with_name(path.name + ".lock")
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{lock} held by another writer for {timeout:.0f}s") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
