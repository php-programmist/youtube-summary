#!/usr/bin/env python3
"""Normalize n8n workflow JSON files for stable git diffs.

Strips volatile fields (versionId, updatedAt, triggerCount, meta.instanceId),
sorts keys recursively and renames files by workflow `name`.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

VOLATILE_TOP = {
    "versionId",
    "updatedAt",
    "triggerCount",
    "activeVersionId",
    "versionCounter",
    "versionMetadata",
    "shared",
}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")
    return s or "workflow"


def normalize(data: dict) -> dict:
    out = {k: v for k, v in data.items() if k not in VOLATILE_TOP}
    meta = out.get("meta")
    if isinstance(meta, dict):
        meta = {k: v for k, v in meta.items() if k != "instanceId"}
        if meta:
            out["meta"] = meta
        else:
            out.pop("meta", None)
    return out


def write(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def cmd_normalize_dir(src: Path, dst: Path) -> None:
    if not src.is_dir():
        print(f"error: source not a directory: {src}", file=sys.stderr)
        sys.exit(1)
    dst.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    for p in sorted(src.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        norm = normalize(data)
        name = norm.get("name") or p.stem
        fname = f"{slugify(name)}.json"
        if fname in written:
            print(f"warn: duplicate slug, overwriting: {fname}", file=sys.stderr)
        written.add(fname)
        write(dst / fname, norm)
        print(f"  {p.name} → {fname}")
    for p in dst.glob("*.json"):
        if p.name not in written:
            p.unlink()
            print(f"  removed stale: {p.name}")


def cmd_normalize_file(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    write(path, normalize(data))
    print(f"normalized: {path}")


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage:\n"
            "  n8n_sync.py normalize-dir SRC DST\n"
            "  n8n_sync.py normalize FILE",
            file=sys.stderr,
        )
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "normalize-dir" and len(sys.argv) == 4:
        cmd_normalize_dir(Path(sys.argv[2]), Path(sys.argv[3]))
    elif cmd == "normalize" and len(sys.argv) == 3:
        cmd_normalize_file(Path(sys.argv[2]))
    else:
        print(f"bad args for command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
