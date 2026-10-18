"""Plain/gzip NDJSON with explicit identity checks and atomic replacements."""
from __future__ import annotations

import gzip
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable


def open_text(path: Path, mode: str):
    if path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def raw_path(output: Path, stem: str, compress: bool = False) -> Path:
    plain = output / "raw" / f"{stem}.ndjson"
    zipped = plain.with_suffix(".ndjson.gz")
    if plain.exists() and zipped.exists():
        raise ValueError(f"Existen dos raw para {stem}; conserva una única versión antes de continuar.")
    if plain.exists():
        return plain
    if zipped.exists():
        return zipped
    return zipped if compress else plain


def read_records(path: Path) -> Iterable[dict[str, Any]]:
    with open_text(path, "r") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number}: NDJSON inválido; conserva el raw y repara la línea antes de reanudar.") from exc
                if not isinstance(record, dict):
                    raise ValueError(f"{path}:{number}: se esperaba un objeto JSON")
                yield record


def unique_records(path: Path, key: Callable, election: int) -> dict[tuple, dict]:
    records = {}
    if not path.exists():
        return records
    for record in read_records(path):
        context = record.get("context", {})
        if context.get("fconvocatoria") != election or context.get("tconvocatoria") != 5:
            raise ValueError(f"{path}: registro de otra convocatoria")
        identity = key(context)
        if identity in records:
            raise ValueError(f"{path}: clave duplicada {identity}")
        records[identity] = record
    return records


def replace_records(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = ".ndjson.gz" if path.suffix == ".gz" else ".ndjson"
    fd, name = tempfile.mkstemp(prefix=".siel-", suffix=suffix, dir=path.parent)
    os.close(fd)
    temp = Path(name)
    try:
        with open_text(temp, "w") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
