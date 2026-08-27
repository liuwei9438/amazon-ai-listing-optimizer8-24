from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import read_workbook


def run(path: Path) -> None:
    envelope = read_workbook(path.name, path.read_bytes())
    fields = envelope.fields
    assert fields.title is not None, "title not detected"
    assert fields.sku is not None, "sku not detected"
    assert len(envelope.records) == len(envelope.dataframe)
    print(
        f"PASS: {path.name} | title={fields.title} | sku={fields.sku} | "
        f"images={fields.images} | bullets={fields.bullets} | records={len(envelope.records)}"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python tests/test_field_detector.py <xlsx-file> [...]")
    for filename in sys.argv[1:]:
        run(Path(filename))
