# 共用文件与任务定义：路径固定在仓库，运行产物采用独占目录和原子 JSON 写入。
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / "benchmarks"


# 使用 UTC 保存时间，避免机器时区改变运行标识和证据解释。
def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# manifest 是实验输入，不读取生成规则来确定字段、数量或标准答案。
def tasks():
    return {task["id"]: task for task in read_json(HOME / "tasks.json")}


def truth_path(task):
    return HOME / "truth" / f"{task['id']}.json"


# 冻结答案的证据也必须完整；源核验不能掩盖本地证据被改动或丢失。
def validate_evidence(task):
    truth = read_json(truth_path(task))
    if truth["task"] != task["id"] or not truth.get("evidence"):
        raise ValueError("Missing ground truth provenance")
    for entry in truth["evidence"]:
        path = HOME / entry["file"]
        if not path.resolve().is_relative_to((HOME / "evidence").resolve()):
            raise ValueError("Invalid evidence path")
        if sha256(path) != entry["sha256"]:
            raise ValueError("Ground truth source evidence changed")
    return truth
