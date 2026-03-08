from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build_dispute_export(
    export_dir: Path,
    subscription: dict,
    evidence_rows: list[dict],
) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    file_name = f"dispute-subscription-{subscription['id']}-{stamp}.zip"
    archive_path = export_dir / file_name

    payload = {
        "generated_at_utc": stamp,
        "subscription": subscription,
        "evidence_timeline": evidence_rows,
    }

    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("dispute.json", json.dumps(payload, ensure_ascii=False, indent=2))

    return archive_path
