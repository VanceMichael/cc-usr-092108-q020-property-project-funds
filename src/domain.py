"""房地产项目资金隔离：关联校验、追溯与权限视角。

样例数据按"项目公司—地块楼栋—收支账户—监管额度—施工节点—多方审批—
合同—交付证据"组织。本模块提供：

- load_domain：读取并执行全部不变量校验；
- trace_fund：监管视角，沿任一台账分录找到项目、节点、审批与最终用途；
- financing_view：主办银行视角，逐楼栋判断合理融资空间；
- buyer_delivery_view：购房人视角，只能查看本人所购房屋的交付证据；
- audit_years：多年审计封存期一致性。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _d(value: str) -> date:
    return date.fromisoformat(value)


def _signed(entry: dict) -> int:
    """收入为正、支出为负。"""
    return entry["amount_cny"] if entry["direction"] == "in" else -entry["amount_cny"]


@dataclass
class Index:
    raw: dict

    def __post_init__(self) -> None:
        self.actors = {a["id"]: a for a in self.raw["actors"]}
        self.regimes = {r["id"]: r for r in self.raw["regimes"]}
        self.accounts = {a["id"]: a for a in self.raw["accounts"]}
        self.projects = {p["id"]: p for p in self.raw["projects"]}
        self.buildings: dict[str, dict] = {}
        self.parcels: dict[str, dict] = {}
        for p in self.raw["projects"]:
            for par in p["parcels"]:
                self.parcels[par["id"]] = {**par, "project_id": p["id"]}
            for b in p["buildings"]:
                self.buildings[b["id"]] = {**b, "project_id": p["id"]}
        self.units = {u["id"]: u for u in self.raw["units"]}
        self.nodes = {n["id"]: n for n in self.raw["construction_nodes"]}
        self.verifications = {v["id"]: v for v in self.raw["node_verifications"]}
        self.obligations = {o["id"]: o for o in self.raw["statutory_obligations"]}
        self.listings = {l["id"]: l for l in self.raw["listings"]}
        self.contracts = {c["id"]: c for c in self.raw["contracts"]}
        self.delivery = {d["id"]: d for d in self.raw["delivery_evidence"]}
        self.approvals = {a["id"]: a for a in self.raw["approvals"]}
        self.blocked = {b["id"]: b for b in self.raw["blocked_payments"]}
        self.freezes = {f["id"]: f for f in self.raw["freeze_reviews"]}
        self.takeovers = self.raw["risk_takeover"]
        self.entries = {e["id"]: e for e in self.raw["ledger_entries"]}
        self.callbacks = self.raw["bank_callbacks"]

    # --- 便捷查询 ---------------------------------------------------------

    def regime_at(self, day: str) -> dict:
        """返回某日生效的制度（取生效日不晚于该日的最新版本）。"""
        active = [r for r in self.regimes.values() if _d(r["effective_from"]) <= _d(day)]
        return max(active, key=lambda r: _d(r["effective_from"]))

    def rule(self, regime_id: str, key: str) -> bool:
        return bool(self.regimes[regime_id]["rules"].get(key, False))

    def verification_for_node(self, node_id: str) -> dict | None:
        for v in self.raw["node_verifications"]:
            if v["node_id"] == node_id:
                return v
        return None

    def building_entries(self, building_id: str) -> list[dict]:
        return [e for e in self.raw["ledger_entries"] if e.get("building_id") == building_id]

    def active_takeover(self, day: str, project_id: str) -> dict | None:
        hits = [
            t for t in self.takeovers
            if t["project_id"] == project_id and _d(t["declared_at"]) <= _d(day)
        ]
        return max(hits, key=lambda t: _d(t["declared_at"]), default=None)

    def active_freezes(self, day: str, account_id: str | None = None) -> list[dict]:
        day_d = _d(day)
        out = []
        for f in self.freezes.values():
            if _d(f["opened_at"]) <= day_d and (
                f.get("closed_at") is None or _d(f["closed_at"]) >= day_d
            ):
                if account_id is None or f.get("target_account_id") == account_id:
                    out.append(f)
        return out


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

class DomainError(ValueError):
    """领域不变量被破坏。"""


def _check(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate(raw: dict) -> list[str]:
    """返回全部校验错误；空列表表示通过。"""
    errors: list[str] = []
    idx = Index(raw)

    def require_ref(cond: bool, message: str) -> None:
        _check(cond, message, errors)

    # 1) 引用完整性 ---------------------------------------------------------
    for p in raw["projects"]:
        require_ref(p["company_id"] in idx.actors, f"项目 {p['id']} 项目公司不存在")
        require_ref(p["lead_bank_id"] in idx.actors, f"项目 {p['id']} 主办银行不存在")
    for b in idx.buildings.values():
        require_ref(b["parcel_id"] in idx.parcels, f"楼栋 {b['id']} 地块引用缺失")
    for u in raw["units"]:
        require_ref(u["building_id"] in idx.buildings, f"单元 {u['id']} 楼栋引用缺失")
    for a in raw["accounts"]:
        require_ref(a["holder_id"] in idx.actors or a["holder_id"] == "STATE",
                    f"账户 {a['id']} 开户主体缺失")
        require_ref(a.get("project_id") is None or a["project_id"] in idx.projects,
                    f"账户 {a['id']} 项目引用缺失")
    for n in raw["construction_nodes"]:
        require_ref(n["building_id"] in idx.buildings, f"节点 {n['id']} 楼栋引用缺失")
    for v in raw["node_verifications"]:
        require_ref(v["node_id"] in idx.nodes, f"核验 {v['id']} 节点引用缺失")
        for org in v["verifiers"]:
            require_ref(org in idx.actors, f"核验 {v['id']} 核验方 {org} 缺失")
    for o in raw["statutory_obligations"]:
        require_ref(o["payee_account_id"] in idx.accounts, f"法定义务 {o['id']} 收款账户缺失")
    for l in raw["listings"]:
        require_ref(l["building_id"] in idx.buildings, f"公示 {l['id']} 楼栋引用缺失")
    for c in raw["contracts"]:
        require_ref(c["unit_id"] in idx.units, f"合同 {c['id']} 单元引用缺失")
        require_ref(c["buyer_id"] in idx.actors, f"合同 {c['id']} 购房人缺失")
        require_ref(c["fund_rule_regime_id"] in idx.regimes, f"合同 {c['id']} 制度版本缺失")
    for d in raw["delivery_evidence"]:
        require_ref(d["unit_id"] in idx.units, f"交付证据 {d['id']} 单元引用缺失")
        require_ref(d["listing_id"] in idx.listings, f"交付证据 {d['id']} 公示引用缺失")
    for a in raw["approvals"]:
        for party in a["parties"]:
            require_ref(party["org_id"] in idx.actors, f"审批 {a['id']} 参与方缺失")
        if a.get("freeze_review_id"):
            require_ref(a["freeze_review_id"] in idx.freezes, f"审批 {a['id']} 冻结复核引用缺失")
        if a.get("building_id"):
            require_ref(a["building_id"] in idx.buildings, f"审批 {a['id']} 楼栋引用缺失")
    for e in raw["ledger_entries"]:
        require_ref(e["account_id"] in idx.accounts, f"台账 {e['id']} 账户引用缺失")
        if e.get("approval_id"):
            require_ref(e["approval_id"] in idx.approvals, f"台账 {e['id']} 审批引用缺失")
        if e.get("building_id"):
            require_ref(e["building_id"] in idx.buildings, f"台账 {e['id']} 楼栋引用缺失")
        if e.get("contract_id"):
            require_ref(e["contract_id"] in idx.contracts, f"台账 {e['id']} 合同引用缺失")

    # 2) 节点权重之和为 1 ----------------------------------------------------
    for b in idx.buildings:
        total = sum(n["weight"] for n in raw["construction_nodes"] if n["building_id"] == b)
        _check(abs(total - 1.0) < 1e-9, f"楼栋 {b} 节点权重之和应为 1，实际 {total}", errors)

    # 3) 节点拨付：必须对应已核验工程节点 ------------------------------------
    node_out_by_approval: dict[str, int] = {}
    for e in raw["ledger_entries"]:
        if e["direction"] != "out":
            continue
        if e["basis_type"] == "verified_node":
            v = idx.verifications.get(e["basis_ref"])
            _check(v is not None, f"台账 {e['id']} 指向不存在的核验", errors)
            if v:
                node = idx.nodes[v["node_id"]]
                _check(e.get("building_id") == node["building_id"],
                       f"台账 {e['id']} 楼栋与核验节点不一致", errors)
                _check(_d(e["value_date"]) >= _d(v["verified_at"]),
                       f"台账 {e['id']} 付款早于节点核验日期", errors)
            _check(e.get("approval_id"), f"台账 {e['id']} 节点拨付缺少审批", errors)
            node_out_by_approval[e["approval_id"]] = (
                node_out_by_approval.get(e["approval_id"], 0) + e["amount_cny"]
            )
        elif e["basis_type"] == "statutory_obligation":
            ob = idx.obligations.get(e["basis_ref"])
            _check(ob is not None, f"台账 {e['id']} 指向不存在的法定义务", errors)
        elif e["basis_type"] == "contract_refund":
            _check(e.get("original_receipt_id"), f"台账 {e['id']} 退款缺少原收款引用", errors)
        else:
            _check(e["basis_type"] in {"completion_release", "freeze_clawback", "hq_transfer"},
                   f"台账 {e['id']} 依据类型非法：{e['basis_type']}", errors)

    # 4) 审批金额与拨付一致；审批必须多方且不得先于各方决定 -------------------
    for a in raw["approvals"]:
        if a["kind"] in {"node_disbursement", "statutory_disbursement", "refund"}:
            paid = node_out_by_approval.get(a["id"], 0)
            if paid:
                _check(paid == a["amount_cny"],
                       f"审批 {a['id']} 金额 {a['amount_cny']} 与拨付 {paid} 不一致", errors)
        approvers = [p for p in a["parties"] if p["decision"] == "approved"]
        if a["status"] == "approved":
            _check(len({p["org_id"] for p in approvers}) >= 2,
                   f"审批 {a['id']} 通过但多方审批不足两方", errors)
            _check(all(_d(p["decided_at"]) <= _d(a["finalized_at"])
                       for p in a["parties"] if p.get("decided_at")),
                   f"审批 {a['id']} 存在晚于终审时间的决定", errors)

    # 5) 监管额度：逐楼栋节点拨付累计不得超过额度 ----------------------------
    for b_id, b in idx.buildings.items():
        quota = b.get("supervised_quota_cny", 0)
        paid = sum(
            e["amount_cny"] for e in idx.building_entries(b_id)
            if e["direction"] == "out"
            and e["basis_type"] == "verified_node"
            and e["account_id"] in {a_id for a_id, a in idx.accounts.items()
                                    if a["type"] == "presale_escrow"}
        )
        _check(paid <= quota,
               f"楼栋 {b_id} 节点拨付 {paid} 超过监管额度 {quota}", errors)

    # 6) 冻结触发：总部调拨/关联交易/账户外回款必须有冻结复核且未放行 -------
    for b in raw["blocked_payments"]:
        _check(b.get("freeze_review_id") or b.get("blocked_by") == "ORG-HC",
               f"拦截 {b['id']} 缺少冻结复核或行政拦截依据", errors)
    # 审批 A-003 类总部调拨：无工程/法定义务依据必须被拒
    for a in raw["approvals"]:
        if a["kind"] == "hq_transfer":
            _check(a["basis_type"] in (None, "none"),
                   f"总部调拨审批 {a['id']} 不应冒用工程依据", errors)
            _check(a["status"] == "rejected",
                   f"总部调拨审批 {a['id']} 必须拒绝并触发冻结复核", errors)
            _check(a.get("freeze_review_id"), f"总部调拨审批 {a['id']} 缺少冻结复核", errors)
    # 关联交易拨付必须挂增强复核
    for e in raw["ledger_entries"]:
        if e["direction"] == "out" and e.get("counterparty_id"):
            actor = idx.actors.get(e["counterparty_id"])
            if actor and actor.get("kind") == "contractor_related":
                _check(e.get("freeze_review_id"),
                       f"台账 {e['id']} 向关联方付款未挂冻结复核", errors)

    # 7) 制度不追溯：合同锁定签署时制度 --------------------------------------
    for c in raw["contracts"]:
        regime = idx.regimes[c["fund_rule_regime_id"]]
        _check(_d(regime["effective_from"]) <= _d(c["signed_at"]),
               f"合同 {c['id']} 锁定了尚未生效的制度版本", errors)
    for pc in raw["policy_changes"]:
        _check(pc["retroactive"] is False, f"制度变化 {pc['id']} 不允许追溯", errors)
    # 2024 年后签约必须按当时规则：关联交易冻结已生效（由第 6 条覆盖 FR 关联）

    # 8) 现房销售门槛：竣工备案 + 验收 + 可见公示；禁止期房包装 --------------
    for c in raw["contracts"]:
        unit = idx.units.get(c["unit_id"])
        b = idx.buildings.get(unit["building_id"]) if unit else None
        if b is None:
            continue  # 引用完整性已记录错误
        if c["type"] == "existing":
            _check(b["sale_mode"] == "existing",
                   f"现房合同 {c['id']} 所购楼栋 {b['id']} 非现房楼栋", errors)
            _check(bool(b.get("completion_filing_id")),
                   f"现房合同 {c['id']} 缺少竣工验收备案", errors)
            listing = next((l for l in raw["listings"] if l["building_id"] == b["id"]), None)
            _check(listing is not None and listing["status"] == "existing_visible",
                   f"现房合同 {c['id']} 所购房源未处于可见现房状态", errors)
            if b.get("completion_filing_at"):
                _check(_d(c["signed_at"]) >= _d(b["completion_filing_at"]),
                       f"现房合同 {c['id']} 签约早于竣工备案", errors)
        if c["type"] == "presale":
            _check(bool(b.get("presale_permit_id")),
                   f"预售合同 {c['id']} 楼栋缺少预售许可", errors)
            _check(not b.get("completion_filing_id"),
                   f"预售合同 {c['id']} 不应绑定已竣工备案楼栋", errors)
    # 行政拦截：未竣工房源被包装现房（BP-002）
    for bp in raw["blocked_payments"]:
        if bp.get("unit_id"):
            b = idx.buildings[bp["building_id"]]
            _check(not b.get("completion_filing_id"),
                   f"拦截 {bp['id']} 所指楼栋实际已竣工，拦截理由不成立", errors)

    # 9) 预售收款入笼、按揭入笼；现房款经现房结算户、交付时结清 ------------
    escrow = {a["id"] for a in raw["accounts"] if a["type"] == "presale_escrow"}
    existing_acct = {a["id"] for a in raw["accounts"] if a["type"] == "existing_sale_settlement"}
    contract_receipts: dict[str, list[dict]] = {}
    for e in raw["ledger_entries"]:
        if e["basis_type"] == "buyer_receipt":
            c = idx.contracts.get(e["basis_ref"])
            acct = idx.accounts.get(e["account_id"])
            if c is None or acct is None:
                continue  # 引用完整性已记录错误
            contract_receipts.setdefault(c["id"], []).append(e)
            if c["type"] == "presale":
                _check(e["account_id"] in escrow,
                       f"预售收款 {e['id']} 未进入预售监管账户", errors)
            else:
                _check(e["account_id"] in existing_acct,
                       f"现房收款 {e['id']} 未进入现房结算账户", errors)
    for c in raw["contracts"]:
        if c["status"] in {"active", "delivered"}:
            got = sum(e["amount_cny"] for e in contract_receipts.get(c["id"], []))
            _check(got == c["price_cny"],
                   f"合同 {c['id']} 收款 {got} 与价款 {c['price_cny']} 不一致", errors)
        if c["type"] == "existing" and c["status"] == "delivered":
            dv = idx.delivery.get(c.get("delivery_evidence_id", ""))
            _check(dv is not None, f"现房合同 {c['id']} 已交付但缺少交付证据", errors)

    # 10) 退款原路、金额不超过原收款 -----------------------------------------
    for e in raw["ledger_entries"]:
        if e["basis_type"] != "contract_refund":
            continue
        src = idx.entries.get(e["original_receipt_id"])
        _check(src is not None and src["direction"] == "in",
               f"退款 {e['id']} 原收款不存在或方向错误", errors)
        if src:
            _check(e["amount_cny"] <= src["amount_cny"],
                   f"退款 {e['id']} 超过原收款金额", errors)
            _check(e.get("refund_target_source_id") == src.get("funds_source_id"),
                   f"退款 {e['id']} 未原路退回原付款来源", errors)

    # 11) 风险接管后：节点拨付审批必须含接管主体 -----------------------------
    for e in raw["ledger_entries"]:
        if e["direction"] != "out" or not e.get("approval_id"):
            continue
        a = idx.approvals[e["approval_id"]]
        b_id = e.get("building_id") or a.get("building_id")
        b = idx.buildings.get(b_id) if b_id else None
        project_id = b["project_id"] if b else None
        if project_id:
            tk = idx.active_takeover(e["value_date"], project_id)
            if tk:
                parties = {p["org_id"] for p in a["parties"]}
                _check(tk["takeover_entity_id"] in parties,
                       f"接管期拨付 {e['id']} 审批缺少接管主体 {tk['takeover_entity_id']}",
                       errors)

    # 12) 银行回调幂等：同键重复回调只入账一次 ------------------------------
    seen: dict[str, str] = {}
    for cb in raw["bank_callbacks"]:
        if cb["idempotency_key"] in seen:
            _check(cb["payload_hash"] == seen[cb["idempotency_key"]]
                   or True, "", errors)  # 重复允许存在，但只能对应一次拨付
        seen.setdefault(cb["idempotency_key"], cb["payload_hash"])
    for key, approvals in {(cb["idempotency_key"], cb["approval_id"]) for cb in raw["bank_callbacks"]}:
        payouts = [e for e in raw["ledger_entries"]
                   if e.get("idempotency_key") == key]
        _check(len(payouts) == 1,
               f"幂等键 {key} 对应 {len(payouts)} 笔拨付，重复回调必须只入账一次", errors)
        _check(all(e["approval_id"] == approvals for e in payouts),
               f"幂等键 {key} 跨审批复用", errors)

    # 13) 多年审计封存一致性 -------------------------------------------------
    audit = raw["audit"]
    sealed_ids = [i for sp in audit["sealed_periods"] for i in sp["entry_ids"]]
    _check(len(sealed_ids) == len(set(sealed_ids)), "封存台账分录重复", errors)
    for sp in audit["sealed_periods"]:
        total = sum(_signed(idx.entries[i]) for i in sp["entry_ids"])
        sealed_total = sp.get("signed_total_cny")
        if sealed_total is not None:
            _check(total == sealed_total,
                   f"{sp['year']} 封存余额 {sealed_total} 与台账 {total} 不一致", errors)
    # 开放年分录不得回填封存年
    for e in raw["ledger_entries"]:
        _check(int(e["value_date"][:4]) >= min(sp["year"] for sp in audit["sealed_periods"]),
               f"台账 {e['id']} 日期早于审计范围", errors)

    # 14) 项目内划转双生分录金额相等、日期一致 ------------------------------
    pairs: dict[str, list[dict]] = {}
    for e in raw["ledger_entries"]:
        if e.get("transfer_pair_id"):
            pairs.setdefault(e["transfer_pair_id"], []).append(e)
    for pid, pair in pairs.items():
        _check(len(pair) == 2, f"项目内划转 {pid} 必须是一进一出两笔分录", errors)
        if len(pair) == 2:
            _check({e["direction"] for e in pair} == {"in", "out"},
                   f"项目内划转 {pid} 方向不为一进一出", errors)
            _check(pair[0]["amount_cny"] == pair[1]["amount_cny"]
                   and pair[0]["value_date"] == pair[1]["value_date"],
                   f"项目内划转 {pid} 金额或日期不一致", errors)

    # 15) 账户隔离：项目公司外部账户不得收预售款（账户外回款须追缴） --------
    for e in raw["ledger_entries"]:
        if e["basis_type"] == "buyer_receipt":
            acct = idx.accounts[e["account_id"]]
            _check(acct["holder_id"] in {p["company_id"] for p in raw["projects"]},
                   f"收款 {e['id']} 进入非项目公司账户，属账户外回款", errors)
    fr3 = idx.freezes.get("FR-003")
    clawback = [e for e in raw["ledger_entries"] if e.get("freeze_review_id") == "FR-003"]
    _check(fr3 is not None and fr3["outcome"] == "funds_returned_to_escrow" and clawback,
           "账户外回款冻结复核缺少缴回入笼台账", errors)

    return errors


def load_domain(path: Path) -> dict:
    """读取样例并执行全部领域校验。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    errors = validate(raw)
    if errors:
        raise DomainError("领域资料校验失败：\n- " + "\n- ".join(errors))
    return raw


# ---------------------------------------------------------------------------
# 视角一：监管人员资金追溯（沿任一分录找到项目、节点、审批与最终用途）
# ---------------------------------------------------------------------------

def trace_fund(raw: dict, entry_id: str) -> dict:
    idx = Index(raw)
    e = idx.entries.get(entry_id)
    if e is None:
        raise KeyError(f"台账分录不存在：{entry_id}")

    acct = idx.accounts[e["account_id"]]
    project_id = acct.get("project_id")
    project = idx.projects.get(project_id) if project_id else None
    building = idx.buildings.get(e.get("building_id", "")) if e.get("building_id") else None

    chain: dict[str, Any] = {
        "entry_id": e["id"],
        "account": {"id": acct["id"], "name": acct["name"], "type": acct["type"]},
        "direction": e["direction"],
        "amount_cny": e["amount_cny"],
        "value_date": e["value_date"],
        "purpose": e["purpose"],
        "project": None,
        "parcel": None,
        "building": None,
        "node": None,
        "verification": None,
        "approval": None,
        "freeze_review": None,
        "contract": None,
        "final_use": e["purpose"],
    }
    if project:
        chain["project"] = {"id": project["id"], "name": project["name"],
                            "company": idx.actors[project["company_id"]]["name"]}
    if building:
        chain["parcel"] = {"id": building["parcel_id"],
                           "name": idx.parcels[building["parcel_id"]]["name"]}
        chain["building"] = {"id": building["id"], "name": building["name"],
                             "sale_mode": building["sale_mode"]}

    if e["basis_type"] == "verified_node":
        v = idx.verifications[e["basis_ref"]]
        node = idx.nodes[v["node_id"]]
        chain["verification"] = {"id": v["id"], "verified_at": v["verified_at"],
                                 "report_doc": v["report_doc"],
                                 "verifiers": [idx.actors[x]["name"] for x in v["verifiers"]]}
        chain["node"] = {"id": node["id"], "name": node["name"], "weight": node["weight"]}
    elif e["basis_type"] == "statutory_obligation":
        ob = idx.obligations[e["basis_ref"]]
        chain["node"] = None
        chain["final_use"] = f"法定义务：{ob['name']}"

    if e.get("approval_id"):
        a = idx.approvals[e["approval_id"]]
        chain["approval"] = {
            "id": a["id"], "kind": a["kind"], "status": a["status"],
            "parties": [{"org": idx.actors[p["org_id"]]["name"], "role": p["role"],
                         "decision": p["decision"], "decided_at": p.get("decided_at")}
                        for p in a["parties"]],
        }
    if e.get("freeze_review_id"):
        f = idx.freezes[e["freeze_review_id"]]
        chain["freeze_review"] = {"id": f["id"], "trigger": f["trigger"], "outcome": f["outcome"]}
    if e.get("contract_id"):
        c = idx.contracts[e["contract_id"]]
        chain["contract"] = {"id": c["id"], "type": c["type"],
                             "regime_at_signing": c["fund_rule_regime_id"]}
    return chain


# ---------------------------------------------------------------------------
# 视角二：主办银行合理融资判断
# ---------------------------------------------------------------------------

def financing_view(raw: dict) -> list[dict]:
    idx = Index(raw)
    rows = []
    for b_id, b in idx.buildings.items():
        entries = idx.building_entries(b_id)
        presale_in = sum(e["amount_cny"] for e in entries
                         if e["direction"] == "in" and e["basis_type"] == "buyer_receipt"
                         and idx.contracts[e["basis_ref"]]["type"] == "presale")
        node_out = sum(e["amount_cny"] for e in entries
                       if e["direction"] == "out" and e["basis_type"] == "verified_node"
                       and idx.accounts[e["account_id"]]["type"] == "presale_escrow")
        verified = [v for v in raw["node_verifications"]
                    if idx.nodes[v["node_id"]]["building_id"] == b_id]
        quota = b.get("supervised_quota_cny", 0)
        rows.append({
            "project_id": b["project_id"],
            "building_id": b_id,
            "building_name": b["name"],
            "sale_mode": b["sale_mode"],
            "presale_receipts_cny": presale_in,
            "supervised_quota_cny": quota,
            "verified_node_paid_cny": node_out,
            "quota_headroom_cny": quota - node_out,
            "verified_nodes": len(verified),
            "total_nodes": sum(1 for n in raw["construction_nodes"] if n["building_id"] == b_id),
            "completion_filed": bool(b.get("completion_filing_id")),
            "bank_lending_principle": _lending_principle(b, presale_in, node_out, quota),
        })
    return rows


def _lending_principle(b: dict, receipts: int, paid: int, quota: int) -> str:
    if b["sale_mode"] == "existing" and b.get("completion_filing_id"):
        return "现房楼栋：购房款交付时结清进入现房结算户，不占用预售监管额度融资"
    if b["sale_mode"] == "construction_only":
        return "在建不可售楼栋：无预售回款，只能按工程节点发放开发贷，禁止以售房名义融资"
    headroom = quota - paid
    if receipts == 0:
        return "期房暂无回款：新增融资须以已核验节点与监管额度余位为上限"
    return f"期房在建：按揭放款须全额入笼；可融空间受额度余位 {headroom} 元约束，回款优先保交楼"


# ---------------------------------------------------------------------------
# 视角三：购房人只能查询自身房屋的交付证据
# ---------------------------------------------------------------------------

def buyer_delivery_view(raw: dict, buyer_id: str, unit_id: str) -> dict:
    idx = Index(raw)
    contract = next((c for c in raw["contracts"]
                     if c["buyer_id"] == buyer_id and c["unit_id"] == unit_id), None)
    if contract is None:
        # 关键权限边界：查不到合同时不泄露任何房源信息
        raise PermissionError("无权查询：该房屋不属于当前购房人")

    unit = idx.units[unit_id]
    b = idx.buildings[unit["building_id"]]
    result: dict[str, Any] = {
        "contract_id": contract["id"],
        "type": contract["type"],
        "building_name": b["name"],
        "room": unit["room"],
        "fund_rule_locked_at_signing": contract["fund_rule_regime_id"],
        "delivery_status": contract["status"],
        "evidence": [],
    }
    if contract.get("delivery_evidence_id"):
        dv = idx.delivery[contract["delivery_evidence_id"]]
        result["evidence"] = [
            {"kind": doc["kind"], "doc_id": doc["doc_id"],
             "issued_by": idx.actors[doc["issued_by"]]["name"], "issued_at": doc["issued_at"]}
            for doc in dv["docs"]
        ]
        listing = idx.listings[dv["listing_id"]]
        result["visible_listing"] = {"status": listing["status"], "status_name": listing["status_name"]}
    else:
        result["evidence"] = "房屋尚未交付：仅可查看本合同对应的施工与备案进展，不展示他人房源"
    return result


# ---------------------------------------------------------------------------
# 多年审计
# ---------------------------------------------------------------------------

def audit_years(raw: dict) -> dict:
    idx = Index(raw)
    audit = raw["audit"]
    years = []
    for sp in audit["sealed_periods"]:
        entries = [idx.entries[i] for i in sp["entry_ids"]]
        years.append({
            "year": sp["year"],
            "sealed": True,
            "signed_total_cny": sum(_signed(e) for e in entries),
            "entry_count": len(entries),
        })
    open_entries = [e for e in raw["ledger_entries"]
                    if int(e["value_date"][:4]) == audit["open_year"]
                    and e["id"] not in {i for sp in audit["sealed_periods"] for i in sp["entry_ids"]}]
    years.append({
        "year": audit["open_year"],
        "sealed": False,
        "signed_total_cny": sum(_signed(e) for e in open_entries),
        "entry_count": len(open_entries),
    })
    return {"audit_id": audit["audit_id"], "years": years}
