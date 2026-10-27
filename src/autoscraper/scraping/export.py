# 抓取数据的文件输出：支持 UTF-8 JSON 和 CSV。
# 字段顺序由调用方传入，列表字段与详情字段合并后的顺序与协议保持一致。
"""UTF-8 exports, preserving protocol field order."""

import csv
import json
from pathlib import Path


# 按扩展名导出数据，创建缺失目录；CSV 列顺序由 fields 指定。
# JSON 保留中文原文，CSV 用 newline='' 避免 Windows 写出额外空行。
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
