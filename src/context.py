"""读取并检查项目领域资料（v2 兼容入口）。

v1 资料只含参与者、事实与约束的字符串清单；v2 起资料扩展为项目公司、
地块楼栋、账户、节点核验、监管额度、审批、合同与交付证据的关联数据。
本入口保留旧调用方式，完整不变量校验委托给 :mod:`src.domain`。
"""

import json
from pathlib import Path

from src.domain import DomainError, validate

_V1_REQUIRED = {"domain", "version", "sample_id", "actors", "facts", "constraints"}
_V2_REQUIRED = {"domain", "version", "sample_id", "actors", "projects", "accounts",
                "ledger_entries", "contracts"}


def load_context(path: Path) -> dict:
    """返回字段完整且通过领域不变量校验的资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = _V1_REQUIRED if value.get("version", 2) < 2 else _V2_REQUIRED
    if not required.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if value["version"] < 1:
        raise ValueError("领域资料内容不完整")
    if value["version"] >= 2:
        errors = validate(value)
        if errors:
            raise DomainError("领域资料校验失败：\n- " + "\n- ".join(errors))
    elif not value["facts"] or not value["constraints"]:
        raise ValueError("领域资料内容不完整")
    return value
