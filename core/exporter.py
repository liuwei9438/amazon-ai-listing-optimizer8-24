from __future__ import annotations

import hashlib

from .models import WorkbookEnvelope


def export_unchanged(envelope: WorkbookEnvelope) -> bytes:
    """Return the exact uploaded workbook bytes.

    This is the V2.2 data-layer contract: until a module explicitly requests a
    controlled workbook edit, exports must remain byte-for-byte identical.
    """
    return envelope.raw_bytes


def integrity_report(envelope: WorkbookEnvelope, exported: bytes) -> dict[str, object]:
    source_hash = hashlib.sha256(envelope.raw_bytes).hexdigest()
    exported_hash = hashlib.sha256(exported).hexdigest()
    return {
        "source_sha256": source_hash,
        "export_sha256": exported_hash,
        "byte_identical": source_hash == exported_hash,
        "source_size": len(envelope.raw_bytes),
        "export_size": len(exported),
    }
