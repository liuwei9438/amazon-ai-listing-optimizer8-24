from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import export_unchanged, integrity_report, read_workbook


def run(path: Path) -> None:
    raw = path.read_bytes()
    envelope = read_workbook(path.name, raw)
    exported = export_unchanged(envelope)
    report = integrity_report(envelope, exported)
    assert report["byte_identical"] is True
    assert len(exported) == len(raw)
    print(f"PASS: {path.name} | {len(raw)} bytes | {envelope.diagnostics['column_count']} columns")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tests/test_integrity.py <xlsx-file>")
    run(Path(sys.argv[1]))
