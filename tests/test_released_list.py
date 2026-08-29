from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

import sheltercheck.released_list as released_list_module
from sheltercheck.released_list import ReleasedListError, ReleasedListService


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def test_missing_file_is_an_empty_command_list(tmp_path: Path) -> None:
    service = ReleasedListService(tmp_path / "missing.txt")
    assert run(service.get()) == ()


def test_atomic_failure_preserves_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "released_today.txt"
    path.write_text("Старий С.С\n", encoding="utf-8")
    service = ReleasedListService(path)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(released_list_module.os, "replace", fail_replace)
    with pytest.raises(ReleasedListError, match="simulated replace failure"):
        run(service.replace(("Новий Н.Н",)))

    assert path.read_text(encoding="utf-8") == "Старий С.С\n"
    assert list(tmp_path.glob(".released_today.txt.*.tmp")) == []


def test_atomic_replacement_uses_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "released_today.txt"
    path.write_text("Старий С.С\n", encoding="utf-8")
    path.chmod(0o666)
    run(ReleasedListService(path).replace(("Новий Н.Н",)))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_concurrent_add_and_set_operations_never_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "released_today.txt"
    path.write_text("Початковий П.П\n", encoding="utf-8")
    service = ReleasedListService(path)

    async def exercise() -> tuple[str, ...]:
        await asyncio.gather(
            service.add(("Доданий Д.Д",)),
            service.replace(("Встановлений В.В",)),
        )
        return await service.get()

    result = run(exercise())
    assert result in {
        ("Встановлений В.В",),
        ("Встановлений В.В", "Доданий Д.Д"),
    }
    assert path.read_text(encoding="utf-8") in {
        "Встановлений В.В\n",
        "Встановлений В.В\nДоданий Д.Д\n",
    }


def test_dry_run_mutations_stay_in_memory(tmp_path: Path) -> None:
    path = tmp_path / "released_today.txt"
    path.write_text("Production P.P\n", encoding="utf-8")
    service = ReleasedListService(path, in_memory=True)

    run(service.replace(("Dry Run D.D",)))

    assert run(service.get()) == ("Dry Run D.D",)
    assert path.read_text(encoding="utf-8") == "Production P.P\n"
