"""FILE_OFFER — манифест до передачи (ТЗ 12.2): хеши всех чанков + полный SHA-256."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.events import new_id


@dataclass
class Manifest:
    file_id: str
    filename: str
    size: int
    chunk_size: int
    chunk_count: int
    full_sha256: str
    chunk_hashes: list[str] = field(default_factory=list)

    def to_dict(self, with_hashes: bool = True) -> dict[str, Any]:
        d = {"file_id": self.file_id, "filename": self.filename, "size": self.size, "chunk_size": self.chunk_size,
             "chunk_count": self.chunk_count, "full_sha256": self.full_sha256}
        if with_hashes:
            d["chunk_hashes"] = self.chunk_hashes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Manifest":
        try:
            m = cls(file_id=str(d["file_id"]), filename=str(d["filename"]), size=int(d["size"]),
                    chunk_size=int(d["chunk_size"]), chunk_count=int(d["chunk_count"]),
                    full_sha256=str(d["full_sha256"]),
                    chunk_hashes=[str(h) for h in d.get("chunk_hashes", [])])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"некорректный манифест: {e}") from e
        if m.size < 0 or m.chunk_size <= 0 or m.chunk_count != chunk_count_for(m.size, m.chunk_size):
            raise ValueError("манифест: несогласованные size/chunk_size/chunk_count")
        if m.chunk_hashes and len(m.chunk_hashes) != m.chunk_count:
            raise ValueError("манифест: число хешей не совпадает с числом чанков")
        return m


def chunk_count_for(size: int, chunk_size: int) -> int:
    return max(1, (size + chunk_size - 1) // chunk_size) if size > 0 else 1


def chunk_range(index: int, size: int, chunk_size: int) -> tuple[int, int]:
    off = index * chunk_size
    return off, min(chunk_size, size - off)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(path: Path, chunk_size: int, file_id: str | None = None) -> Manifest:
    """Блокирующая операция (SHA-256 гигабайта считается заметное время) — вызывать в executor."""
    size = path.stat().st_size
    count = chunk_count_for(size, chunk_size)
    full = hashlib.sha256()
    hashes: list[str] = []
    with open(path, "rb") as f:
        for _ in range(count):
            data = f.read(chunk_size)
            full.update(data)
            hashes.append(sha256_hex(data))
    return Manifest(file_id=file_id or new_id(), filename=path.name, size=size, chunk_size=chunk_size,
                    chunk_count=count, full_sha256=full.hexdigest(), chunk_hashes=hashes)


def file_sha256(path: Path, block: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(block)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def verify_existing_chunks(path: Path, manifest: Manifest) -> list[int]:
    """Какие чанки во временном файле уже целы (resume после перезапуска). Блокирующая."""
    ok: list[int] = []
    if not path.exists():
        return ok
    with open(path, "rb") as f:
        for i, h in enumerate(manifest.chunk_hashes):
            off, ln = chunk_range(i, manifest.size, manifest.chunk_size)
            f.seek(off)
            data = f.read(ln)
            if len(data) == ln and sha256_hex(data) == h:
                ok.append(i)
    return ok
