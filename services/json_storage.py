from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any = None):
    """Safely read JSON. Return default when the file is absent or unreadable."""
    if default is None:
        default = {}

    if not path.exists():
        return default

    try:
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return default
        return json.loads(content)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data: Any):
    """Atomically write JSON using a unique same-directory temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")

    try:
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
