from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import Member
from .roster import Roster, released_members_from_names


class ReleasedListError(RuntimeError):
    """The released-list file could not be read or replaced safely."""


@dataclass(frozen=True, slots=True)
class AddResult:
    added: int
    already_present: int
    total: int


@dataclass(frozen=True, slots=True)
class RemoveResult:
    removed: int
    absent: int
    total: int


class ReleasedListService:
    """Serialize released-list changes and atomically publish complete files."""

    def __init__(self, path: str | Path, *, in_memory: bool = False) -> None:
        self.path = Path(path)
        self._in_memory = in_memory
        self._memory_names: tuple[str, ...] | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> tuple[str, ...]:
        async with self._lock:
            return self._get_unlocked(missing_ok=True)

    async def get_members(
        self, roster: Roster, *, missing_ok: bool = False
    ) -> tuple[Member, ...]:
        async with self._lock:
            names = self._get_unlocked(missing_ok=missing_ok)
            return released_members_from_names(names, roster)

    async def replace(self, names: Iterable[str]) -> int:
        replacement = _clean_names(names)
        async with self._lock:
            self._write_unlocked(replacement)
            return len(replacement)

    async def add(self, names: Iterable[str]) -> AddResult:
        additions = _clean_names(names)
        async with self._lock:
            current = self._get_unlocked(missing_ok=True)
            current_set = set(current)
            new_names = tuple(name for name in additions if name not in current_set)
            if new_names:
                self._write_unlocked((*current, *new_names))
            return AddResult(
                added=len(new_names),
                already_present=len(additions) - len(new_names),
                total=len(current) + len(new_names),
            )

    async def remove(self, names: Iterable[str]) -> RemoveResult:
        removals = _clean_names(names)
        async with self._lock:
            current = self._get_unlocked(missing_ok=True)
            current_set = set(current)
            removal_set = set(removals)
            remaining = tuple(name for name in current if name not in removal_set)
            removed = sum(name in current_set for name in removals)
            if removed:
                self._write_unlocked(remaining)
            return RemoveResult(
                removed=removed,
                absent=len(removals) - removed,
                total=len(remaining),
            )

    async def clear(self) -> None:
        async with self._lock:
            self._write_unlocked(())

    def _get_unlocked(self, *, missing_ok: bool) -> tuple[str, ...]:
        if self._in_memory:
            if self._memory_names is None:
                self._memory_names = self._read_file(missing_ok=True)
            return self._memory_names
        return self._read_file(missing_ok=missing_ok)

    def _read_file(self, *, missing_ok: bool) -> tuple[str, ...]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            if missing_ok:
                return ()
            raise ReleasedListError(
                f"cannot read released list {self.path}: {exc}"
            ) from exc
        except (OSError, UnicodeError) as exc:
            raise ReleasedListError(
                f"cannot read released list {self.path}: {exc}"
            ) from exc
        return tuple(line.strip() for line in lines if line.strip())

    def _write_unlocked(self, names: tuple[str, ...]) -> None:
        if self._in_memory:
            self._memory_names = names
            return
        _atomic_write(self.path, names)


def _clean_names(names: Iterable[str]) -> tuple[str, ...]:
    cleaned = tuple(name.strip() for name in names if name.strip())
    if len(cleaned) != len(set(cleaned)):
        raise ReleasedListError("released-list update contains duplicate display names")
    return cleaned


def _atomic_write(path: Path, names: tuple[str, ...]) -> None:
    """Write a complete 0600 temporary file, fsync it, then atomically replace."""

    content = "\n".join(names)
    if names:
        content += "\n"

    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, UnicodeError) as exc:
        raise ReleasedListError(f"cannot update released list {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
