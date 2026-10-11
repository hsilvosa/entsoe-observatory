"""Directory swap used to make layer writes idempotent."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


def swap_directory(new_dir: Path, final_dir: Path) -> None:
    """Replace final_dir with new_dir. The old content is removed only after the move."""
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = final_dir.with_name(final_dir.name + ".old")
    if backup.exists():
        shutil.rmtree(backup)
    if final_dir.exists():
        final_dir.rename(backup)
    new_dir.rename(final_dir)
    if backup.exists():
        shutil.rmtree(backup)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
