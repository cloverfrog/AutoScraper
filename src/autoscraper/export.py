"""UTF-8 exports, preserving protocol field order."""

import csv
import json
from pathlib import Path


def export_records(records: list[dict], path: Path, fields: list[str]) -> None:
    if path.suffix.lower() not in {".csv", ".json"}:
        raise ValueError("Output extension must be .csv or .json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        if path.suffix.lower() == ".json":
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        else:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
