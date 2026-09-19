"""Карантин, санитизация путей, atomic rename (ТЗ 12.8). Санитизируется ПУТЬ, а не алфавит имени."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"|?*\\/]')   # запрещённые в именах Windows + разделители путей
MAX_NAME_BYTES = 200


def sanitize_filename(name: str) -> str:
    """«Домашка №3.zip» остаётся собой; «../../x.exe» и «C:\\Windows\\..» превращаются в безопасное имя."""
    name = name.replace("\\", "/").split("/")[-1]      # берём только последний компонент пути
    name = _BAD_CHARS.sub("_", name)
    name = name.strip().strip(".")                      # Windows не любит точки/пробелы по краям
    if not name or name in (".", ".."):
        name = "file"
    stem, dot, ext = name.partition(".")
    if stem.upper() in _RESERVED:
        name = f"_{name}"
    raw = name.encode("utf-8")
    if len(raw) > MAX_NAME_BYTES:
        base, ext = os.path.splitext(name)
        ext = ext[:16]
        while len((base + ext).encode("utf-8")) > MAX_NAME_BYTES and base:
            base = base[:-1]
        name = base + ext
    return name


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / filename
    if not p.exists():
        return p
    base, ext = os.path.splitext(filename)
    n = 1
    while True:
        p = directory / f"{base} ({n}){ext}"
        if not p.exists():
            return p
        n += 1


def free_space(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def storage_usage(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def atomic_move(tmp: Path, final: Path) -> Path:
    """Файл появляется в files/ только целиком (после проверки SHA-256)."""
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(tmp, final)
    except OSError:
        # другой диск/раздел: копируем, затем переименовываем
        staged = final.with_name(final.name + ".part")
        shutil.copy2(tmp, staged)
        os.replace(staged, final)
        tmp.unlink(missing_ok=True)
    return final


def prepare_temp(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size != size:
        with open(path, "r+b" if path.exists() else "wb") as f:
            f.truncate(size)
