"""读取并检查项目领域资料。"""

import json
from pathlib import Path

def load_context(path: Path) -> dict:
    """返回字段完整的领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"domain", "version", "sample_id", "actors", "facts", "constraints"}
    if not required.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if value["version"] < 1 or not value["facts"] or not value["constraints"]:
        raise ValueError("领域资料内容不完整")
    return value
