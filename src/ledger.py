"""房地产项目资金隔离的后台关系模型与一致性校验。

本模块描述项目公司独立账、地块楼栋、收支账户、监管额度与销售交付
之间的后台关系，并提供以下能力：

- validate_ledger：校验一份台账是否满足全部领域不变量；
- apply_bank_callback：按幂等规则处理银行入账回调（重复回调只记录）；
- buyer_view：购房人只能查询自身房屋的交付证据；
- bank_view：主办银行据此判断合理融资的项目视图；
- trace：监管人员沿任一资金流找到项目、节点、审批与最终用途。

约定：金额一律为整数（人民币元），日期为 ISO 字符串，台账为纯 dict，
样例见 fixtures/ledger.json，结构契约见 contracts/ledger.schema.json。
"""

import hashlib
import json

# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------

ACCOUNT_KINDS = ("supervised", "general", "external")

PAYMENT_TYPES = (
    "milestone",      # 对应已核验工程节点
    "obligation",     # 对应法定义务（税费、农民工工资专户等）
    "refund",         # 退房退款
    "hq_transfer",    # 总部调拨（触发冻结复核）
    "related_party",  # 关联交易（触发冻结复核）
)

FREEZE_TRIGGERS = ("hq_transfer", "related_party", "off_account_receipt", "takeover")

FREEZE_RESULTS = ("rejected", "returned", "released", "pending")

TAKEOVER_PERMISSIONS = ("normal", "committee_only", "frozen")

# 各付款类型必须满足的最低条件
PAYMENT_RULES = {
    "milestone": {"needs": "milestone"},
    "obligation": {"needs": "obligation"},
    "refund": {"needs": "refund"},
    "hq_transfer": {"needs": "freeze"},
    "related_party": {"needs": "freeze"},
}

TOP_LEVEL_KEYS = (
    "as_of", "policies", "project", "company", "parcels", "buildings",
    "units", "milestones", "obligations", "accounts", "payees",
    "contracts", "receipts", "payments", "approvals", "callbacks",
    "freeze_reviews", "takeovers", "deliveries", "audit_events",
)


class LedgerError(ValueError):
    """台账违反领域不变量。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _require(cond, errors, message):
    if not cond:
        errors.append(message)


def _index(rows, key="id"):
    return {row[key]: row for row in rows}


def _ids(rows):
    return {row["id"] for row in rows}


def _sorted_events(ledger):
    """把全部资金事件按时间排序，用于余额时序校验。"""
    events = []
    for r in ledger["receipts"]:
        events.append((r["date"], "receipt", r))
    for p in ledger["payments"]:
        events.append((p["date"], "payment", p))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "receipt" else 1, e[2]["id"]))
    return events


def _policy_at(policies, date):
    """签署日生效的制度版本；制度变化不追溯改写旧合同。"""
    chosen = None
    for pol in sorted(policies, key=lambda p: p["effective_from"]):
        if pol["effective_from"] <= date:
            chosen = pol
    return chosen


def contract_policy(ledger, contract):
    """合同继续遵循签署时的资金规则：返回其制度快照。"""
    return _policy_at(ledger["policies"], contract["sign_date"])


def _buyer_paid_total(ledger, contract_id):
    """购房人实际缴入项目账户的净额（不含账户外回款）。"""
    total = 0
    for r in ledger["receipts"]:
        if r.get("contract_id") != contract_id:
            continue
        if r["kind"] == "sale" and not r.get("off_account"):
            total += r["amount"]
        if r["kind"] == "correction":
            total += r["amount"]
    return total


def _refund_total(ledger, contract_id):
    return sum(
        p["amount"] for p in ledger["payments"]
        if p["type"] == "refund"
        and p.get("contract_id") == contract_id
        and p["status"] == "executed"
    )


def _milestone_paid_total(ledger, milestone_id):
    return sum(
        p["amount"] for p in ledger["payments"]
        if p["type"] == "milestone"
        and p.get("milestone_id") == milestone_id
        and p["status"] == "executed"
    )


def audit_hash(prev_hash, event):
    """审计事件哈希链：任何篡改都会断链。"""
    payload = {k: v for k, v in event.items() if k != "hash"}
    payload["prev_hash"] = prev_hash
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 主校验入口
# ---------------------------------------------------------------------------

def validate_ledger(ledger):
    """校验台账，返回错误列表；空列表表示全部不变量成立。"""
    errors = []
    _check_structure(ledger, errors)
    if errors:
        return errors
    idx = _build_index(ledger, errors)
    if errors:
        return errors
    _check_entities(ledger, idx, errors)
    _check_contracts(ledger, idx, errors)
    _check_receipts(ledger, idx, errors)
    _check_payments(ledger, idx, errors)
    _check_callbacks(ledger, idx, errors)
    _check_freezes(ledger, idx, errors)
    _check_takeovers(ledger, idx, errors)
    _check_deliveries(ledger, idx, errors)
    _check_balances(ledger, idx, errors)
    _check_audit_chain(ledger, errors)
    return errors


def _check_structure(ledger, errors):
    for key in TOP_LEVEL_KEYS:
        _require(key in ledger, errors, f"台账缺少顶层字段 {key}")
    if errors:
        return
    for key in TOP_LEVEL_KEYS:
        if key in ("as_of", "project", "company"):
            continue
        _require(isinstance(ledger[key], list), errors, f"{key} 必须是数组")


def _build_index(ledger, errors):
    idx = {}
    for key in ("policies", "parcels", "buildings", "units", "milestones",
                "obligations", "accounts", "payees", "contracts", "receipts",
                "payments", "callbacks", "freeze_reviews", "takeovers",
                "deliveries", "audit_events"):
        rows = ledger[key]
        seen = _ids(rows)
        _require(len(seen) == len(rows), errors, f"{key} 存在重复 id")
        idx[key] = _index(rows)
    return idx


def _check_entities(ledger, idx, errors):
    """项目公司独立账与地块楼栋层级。"""
    company = ledger["company"]
    project = ledger["project"]
    _require(project["company_id"] == company["id"], errors,
             "项目必须归属唯一项目公司")
    _require(company["type"] == "project_company", errors,
             "项目公司必须是独立法人")

    for parcel in ledger["parcels"]:
        _require(parcel["project_id"] == project["id"], errors,
                 f"地块 {parcel['id']} 必须归属本项目")
    for b in ledger["buildings"]:
        _require(b["parcel_id"] in idx["parcels"], errors,
                 f"楼栋 {b['id']} 引用了不存在的地块")
    for u in ledger["units"]:
        _require(u["building_id"] in idx["buildings"], errors,
                 f"房源 {u['id']} 引用了不存在的楼栋")

    # 收支账户：监管户与一般户必须开立在项目公司名下
    for a in ledger["accounts"]:
        if a["kind"] in ("supervised", "general"):
            _require(a["owner_company_id"] == company["id"], errors,
                     f"账户 {a['id']} 必须开立在项目公司名下（独立账）")
    kinds = [a["kind"] for a in ledger["accounts"]]
    _require(kinds.count("supervised") == 1, errors, "项目必须有且仅有一个监管账户")
    _require(kinds.count("general") == 1, errors, "项目必须有且仅有一个一般账户")

    # 工程节点归属楼栋；已核验节点必须有核验日期与证明
    for m in ledger["milestones"]:
        _require(m["building_id"] in idx["buildings"], errors,
                 f"节点 {m['id']} 引用了不存在的楼栋")
        if m["certified_by"]:
            _require(m.get("certified_date"), errors,
                     f"节点 {m['id']} 缺少核验日期")
            _require(m.get("certificate_ref"), errors,
                     f"节点 {m['id']} 缺少核验证明编号")
        _require(m["budget_cap"] > 0, errors, f"节点 {m['id']} 预算额度必须为正")

    for o in ledger["obligations"]:
        _require(o["project_id"] == project["id"], errors,
                 f"法定义务 {o['id']} 必须归属本项目")
        _require(o["amount"] > 0, errors, f"法定义务 {o['id']} 金额必须为正")

    for b in ledger["buildings"]:
        acc = b.get("acceptance")
        if acc:
            _require(acc.get("date") and acc.get("doc_ref"), errors,
                     f"楼栋 {b['id']} 竣工验收缺少日期或备案编号")


def _check_contracts(ledger, idx, errors):
    """销售合同：预售/现售规则与制度快照。"""
    for c in ledger["contracts"]:
        _require(c["unit_id"] in idx["units"], errors,
                 f"合同 {c['id']} 引用了不存在的房源")
        unit = idx["units"].get(c["unit_id"])
        if not unit:
            continue
        building = idx["buildings"][unit["building_id"]]
        policy = contract_policy(ledger, c)
        _require(policy is not None, errors,
                 f"合同 {c['id']} 签署日没有生效的制度版本")
        if policy is None:
            continue
        c["policy_id"] = policy["id"]  # 记录快照，供查询与测试

        if c["sale_kind"] == "presale":
            _require(policy["presale_allowed"], errors,
                     f"合同 {c['id']} 签署时制度不允许预售")
            _require(c.get("presale_approval"), errors,
                     f"预售合同 {c['id']} 缺少预售审批编号")
        else:  # finished 现房销售
            _require(policy["finished_sale_allowed"], errors,
                     f"合同 {c['id']} 签署时制度不允许现房销售")
            acc = building.get("acceptance")
            _require(bool(acc), errors,
                     f"现房合同 {c['id']} 绑定楼栋 {building['id']} 尚未竣工验收")
            if acc:
                _require(acc["date"] <= c["sign_date"], errors,
                         f"现房合同 {c['id']} 签署早于楼栋竣工验收")
            _require(unit.get("visible_status") == "listed", errors,
                     f"现房合同 {c['id']} 绑定房源未处于可见在售状态")
            if unit.get("listed_at"):
                _require(unit["listed_at"] <= c["sign_date"], errors,
                         f"现房合同 {c['id']} 签署早于房源上架日期")

        # 监管比例按签署时制度执行，不追溯
        ratio = policy["supervision_ratio"]
        sale_rows = [
            r for r in ledger["receipts"]
            if r.get("contract_id") == c["id"]
            and r["kind"] == "sale" and not r.get("off_account")
        ]
        total_sale = sum(r["amount"] for r in sale_rows)
        total_supervised = sum(r["supervised_amount"] for r in sale_rows)
        _require(total_supervised == total_sale * ratio // 100, errors,
                 f"合同 {c['id']} 监管入账比例与签署时制度（{ratio}%）不符")


def _check_receipts(ledger, idx, errors):
    supervised = [a for a in ledger["accounts"] if a["kind"] == "supervised"][0]
    general = [a for a in ledger["accounts"] if a["kind"] == "general"][0]
    for r in ledger["receipts"]:
        _require(r["amount"] > 0, errors, f"回款 {r['id']} 金额必须为正")
        _require(r["account_id"] in idx["accounts"], errors,
                 f"回款 {r['id']} 引用了不存在的账户")
        if r["kind"] == "sale":
            _require(r.get("contract_id") in idx["contracts"], errors,
                     f"销售回款 {r['id']} 必须关联购房合同")
        if r["kind"] == "correction":
            _require(r.get("corrects") in idx["receipts"], errors,
                     f"纠正入账 {r['id']} 必须引用原账户外回款")
        # 账户外回款不进入任何项目账户；其余按资金性质路由
        if r.get("off_account"):
            _require(r["supervised_amount"] == 0 and r["general_amount"] == 0,
                     errors, f"账户外回款 {r['id']} 不得计入项目账户")
            _require(r["account_id"] not in (supervised["id"], general["id"]),
                     errors, f"账户外回款 {r['id']} 不得进入项目监管户或一般户")
            hit = any(
                f["trigger"] == "off_account_receipt"
                and f.get("receipt_id") == r["id"]
                for f in ledger["freeze_reviews"]
            )
            _require(hit, errors, f"账户外回款 {r['id']} 未触发冻结复核")
            continue
        _require(r["supervised_amount"] + r["general_amount"] == r["amount"],
                 errors, f"回款 {r['id']} 监管/一般拆分金额不等于总额")
        _require(
            (r["supervised_amount"] == r["amount"])
            ^ (r["general_amount"] == r["amount"]),
            errors, f"回款 {r['id']} 一次入账只能进入一个账户（按比例拆行）"
        )
        if r["kind"] == "loan":
            _require(r["account_id"] == general["id"], errors,
                     f"开发贷 {r['id']} 必须进入项目公司一般账户")
            _require(r["general_amount"] == r["amount"], errors,
                     f"开发贷 {r['id']} 不得计入监管资金")
        elif r["supervised_amount"]:
            _require(r["account_id"] == supervised["id"], errors,
                     f"回款 {r['id']} 的监管资金必须进入监管账户")
        else:
            _require(r["account_id"] == general["id"], errors,
                     f"回款 {r['id']} 的非监管资金必须进入一般账户")


def _check_payments(ledger, idx, errors):
    accounts = idx["accounts"]
    for p in ledger["payments"]:
        _require(p["type"] in PAYMENT_TYPES, errors,
                 f"付款 {p['id']} 类型非法")
        _require(p["amount"] > 0, errors, f"付款 {p['id']} 金额必须为正")
        _require(p["account_id"] in accounts, errors,
                 f"付款 {p['id']} 引用了不存在的账户")
        _require(p["payee_id"] in idx["payees"], errors,
                 f"付款 {p['id']} 引用了不存在的收款方")
        if p["account_id"] in accounts:
            _require(accounts[p["account_id"]]["kind"] != "external", errors,
                     f"付款 {p['id']} 不得从外部账户发起")
        payee = idx["payees"].get(p["payee_id"])
        if payee and p["type"] in ("hq_transfer", "related_party"):
            _require(payee.get("related") or payee.get("is_hq"), errors,
                     f"付款 {p['id']} 类型与收款方属性不符")

        need = PAYMENT_RULES[p["type"]]["needs"]
        if need == "milestone":
            m = idx["milestones"].get(p.get("milestone_id"))
            _require(m is not None, errors,
                     f"工程付款 {p['id']} 必须对应工程节点")
            if m:
                _require(bool(m["certified_by"]), errors,
                         f"付款 {p['id']} 对应节点 {m['id']} 尚未核验")
                if m["certified_by"] and m.get("certified_date"):
                    _require(m["certified_date"] <= p["date"], errors,
                             f"付款 {p['id']} 早于节点 {m['id']} 核验日期")
        elif need == "obligation":
            o = idx["obligations"].get(p.get("obligation_id"))
            _require(o is not None, errors,
                     f"法定义务付款 {p['id']} 必须对应法定义务")
            if o:
                _require(o["due_date"] >= p["date"], errors,
                         f"付款 {p['id']} 晚于法定义务 {o['id']} 到期日")
        elif need == "refund":
            cid = p.get("contract_id")
            c = idx["contracts"].get(cid)
            _require(c is not None, errors,
                     f"退款 {p['id']} 必须关联购房合同")
            if c:
                _require(c["status"] == "rescinded", errors,
                         f"退款 {p['id']} 对应合同 {cid} 未解除")
                limit = _buyer_paid_total(ledger, cid)
                _require(p["amount"] <= limit, errors,
                         f"退款 {p['id']} 超过购房人实缴金额")
        elif need == "freeze":
            hit = any(
                f.get("payment_id") == p["id"] for f in ledger["freeze_reviews"]
            )
            _require(hit, errors,
                     f"{p['type']} 付款 {p['id']} 未触发冻结复核")

        # 多方审批
        if p["status"] == "executed":
            _check_approvals(ledger, p, errors)
        else:
            _require(p["status"] in ("rejected", "frozen"), errors,
                     f"付款 {p['id']} 状态非法")
            if p["status"] == "rejected":
                _require(any(
                    a["payment_id"] == p["id"] and a["decision"] == "reject"
                    for a in ledger["approvals"]
                ), errors, f"被拒付款 {p['id']} 缺少拒绝审批记录")

    # 节点付款不得超过该节点预算额度
    for m in ledger["milestones"]:
        paid = _milestone_paid_total(ledger, m["id"])
        _require(paid <= m["budget_cap"], errors,
                 f"节点 {m['id']} 累计付款 {paid} 超过预算额度 {m['budget_cap']}")

    # 同一法定义务不得重复付款
    for o in ledger["obligations"]:
        pays = [p for p in ledger["payments"]
                if p.get("obligation_id") == o["id"] and p["status"] == "executed"]
        _require(len(pays) <= 1, errors,
                 f"法定义务 {o['id']} 被重复付款")
        if pays:
            _require(pays[0]["amount"] <= o["amount"], errors,
                     f"法定义务 {o['id']} 付款超过应付金额")

    # 同一合同退款总额不得超过实缴
    for c in ledger["contracts"]:
        refunded = _refund_total(ledger, c["id"])
        _require(refunded <= _buyer_paid_total(ledger, c["id"]), errors,
                 f"合同 {c['id']} 累计退款超过购房人实缴")


def _check_approvals(ledger, payment, errors):
    """已执行付款必须完成多方审批（企业、监理、银行）。"""
    roles = set()
    for a in ledger["approvals"]:
        if a["payment_id"] == payment["id"] and a["decision"] == "approve":
            roles.add(a["role"])
            _require(a["date"] <= payment["date"], errors,
                     f"付款 {payment['id']} 的审批晚于付款执行日")
    for role in ("developer", "supervisor", "bank"):
        _require(role in roles, errors,
                 f"付款 {payment['id']} 缺少 {role} 审批")


def _check_callbacks(ledger, idx, errors):
    """银行回调：同一付款的重复回调必须被标记忽略，不得重复入账。"""
    seen = {}
    for cb in ledger["callbacks"]:
        _require(cb["payment_id"] in idx["payments"], errors,
                 f"回调 {cb['id']} 引用了不存在的付款")
        key = (cb["payment_id"], cb["bank_ref"])
        if key in seen:
            _require(cb["status"] == "ignored_duplicate", errors,
                     f"重复回调 {cb['id']} 必须标记为 ignored_duplicate")
        else:
            seen[key] = cb
            _require(cb["status"] == "applied", errors,
                     f"首次回调 {cb['id']} 状态必须为 applied")
    for p in ledger["payments"]:
        if p["status"] == "executed":
            hit = any(
                cb["payment_id"] == p["id"] and cb["status"] == "applied"
                for cb in ledger["callbacks"]
            )
            _require(hit, errors, f"已执行付款 {p['id']} 缺少银行回调确认")


def _check_freezes(ledger, idx, errors):
    for f in ledger["freeze_reviews"]:
        _require(f["trigger"] in FREEZE_TRIGGERS, errors,
                 f"冻结复核 {f['id']} 触发原因非法")
        _require(f["result"] in FREEZE_RESULTS, errors,
                 f"冻结复核 {f['id']} 结论非法")
        if f.get("payment_id"):
            p = idx["payments"].get(f["payment_id"])
            _require(p is not None, errors,
                     f"冻结复核 {f['id']} 引用了不存在的付款")
            if p:
                if f["result"] in ("rejected", "returned"):
                    _require(p["status"] == "rejected", errors,
                             f"冻结复核 {f['id']} 已拒绝，但付款 {p['id']} 未拒绝")
                if f["result"] == "pending":
                    _require(p["status"] == "frozen", errors,
                             f"冻结复核 {f['id']} 未结案，付款 {p['id']} 应保持冻结")
        if f["trigger"] == "off_account_receipt":
            r = idx["receipts"].get(f.get("receipt_id"))
            _require(r is not None and r.get("off_account"), errors,
                     f"冻结复核 {f['id']} 必须引用账户外回款")
            if f["result"] == "returned":
                corrected = any(
                    x["kind"] == "correction" and x.get("corrects") == r["id"]
                    for x in ledger["receipts"]
                )
                _require(corrected, errors,
                         f"账户外回款 {r['id']} 被要求退回专户但缺少纠正入账")


def _check_takeovers(ledger, idx, errors):
    for t in ledger["takeovers"]:
        _require(t["permission"] in TAKEOVER_PERMISSIONS, errors,
                 f"风险接管 {t['id']} 权限级别非法")
        _require(t["start_date"] <= ledger["as_of"], errors,
                 f"风险接管 {t['id']} 起始日晚于台账截止日")
        if t.get("end_date"):
            _require(t["end_date"] >= t["start_date"], errors,
                     f"风险接管 {t['id']} 结束日早于起始日")
    for p in ledger["payments"]:
        if p["status"] != "executed":
            continue
        for t in ledger["takeovers"]:
            active = t["start_date"] <= p["date"] and (
                not t.get("end_date") or p["date"] <= t["end_date"]
            )
            if not active:
                continue
            if t["permission"] == "frozen":
                errors.append(
                    f"接管 {t['id']} 冻结期内不得执行付款 {p['id']}")
            elif t["permission"] == "committee_only":
                hit = any(
                    a["payment_id"] == p["id"] and a["role"] == "committee"
                    and a["decision"] == "approve"
                    for a in ledger["approvals"]
                )
                _require(hit, errors,
                         f"接管期内付款 {p['id']} 缺少风险处置专班审批")


def _check_deliveries(ledger, idx, errors):
    for d in ledger["deliveries"]:
        c = idx["contracts"].get(d["contract_id"])
        _require(c is not None, errors, f"交付 {d['id']} 引用了不存在的合同")
        if not c:
            continue
        _require(c["status"] == "performed", errors,
                 f"交付 {d['id']} 对应合同 {c['id']} 未履行完毕")
        unit = idx["units"][c["unit_id"]]
        building = idx["buildings"][unit["building_id"]]
        acc = building.get("acceptance")
        _require(bool(acc), errors,
                 f"交付 {d['id']} 对应楼栋 {building['id']} 尚未竣工验收")
        if acc:
            _require(acc["date"] <= d["date"], errors,
                     f"交付 {d['id']} 早于楼栋竣工验收")
        _require(d.get("evidence_ref"), errors,
                 f"交付 {d['id']} 缺少交付证据编号")


def _check_balances(ledger, idx, errors):
    """账户余额时序：任何时点不得透支；监管账户余额不得低于应留存额度。"""
    balances = {a["id"]: 0 for a in ledger["accounts"]}
    for date, kind, ev in _sorted_events(ledger):
        if kind == "receipt":
            if ev.get("off_account"):
                continue  # 账户外回款不计入任何项目账户
            balances[ev["account_id"]] += ev["amount"]
        else:
            if ev["status"] != "executed":
                continue
            balances[ev["account_id"]] -= ev["amount"]
        for acc_id, bal in balances.items():
            _require(bal >= 0, errors,
                     f"账户 {acc_id} 在 {date} 出现透支（{bal}）")
    for a in ledger["accounts"]:
        a["closing_balance"] = balances[a["id"]]


def _check_audit_chain(ledger, errors):
    prev = "GENESIS"
    for ev in ledger["audit_events"]:
        _require(ev.get("prev_hash") == prev, errors,
                 f"审计事件 {ev['id']} 哈希链断裂")
        expect = audit_hash(prev, ev)
        _require(ev.get("hash") == expect, errors,
                 f"审计事件 {ev['id']} 哈希被篡改")
        prev = ev.get("hash", prev)


# ---------------------------------------------------------------------------
# 银行回调（幂等）
# ---------------------------------------------------------------------------

def apply_bank_callback(ledger, callback):
    """处理一条银行回调；重复回调只记录，不改变已入账结果。"""
    callback = dict(callback)
    key = (callback["payment_id"], callback["bank_ref"])
    for existing in ledger["callbacks"]:
        if (existing["payment_id"], existing["bank_ref"]) == key:
            callback["status"] = "ignored_duplicate"
            ledger["callbacks"].append(callback)
            return callback
    callback["status"] = "applied"
    ledger["callbacks"].append(callback)
    return callback


# ---------------------------------------------------------------------------
# 三类查询视图
# ---------------------------------------------------------------------------

def buyer_view(ledger, contract_id, buyer=None):
    """购房人只能查询自身房屋的交付证据，看不到他人与资金明细。"""
    contracts = _index(ledger["contracts"])
    c = contracts.get(contract_id)
    if c is None:
        raise LedgerError(f"合同 {contract_id} 不存在")
    if buyer is not None and buyer != c["buyer"]:
        raise LedgerError("无权查询他人房屋的交付证据")
    units = _index(ledger["units"])
    buildings = _index(ledger["buildings"])
    unit = units[c["unit_id"]]
    building = buildings[unit["building_id"]]
    deliveries = [d for d in ledger["deliveries"] if d["contract_id"] == c["id"]]
    policy = contract_policy(ledger, c)
    return {
        "contract_id": c["id"],
        "buyer": c["buyer"],
        "unit": unit["name"],
        "building": building["name"],
        "sale_kind": c["sale_kind"],
        "status": c["status"],
        "acceptance": building.get("acceptance"),
        "deliveries": [
            {"date": d["date"], "evidence_ref": d["evidence_ref"]}
            for d in deliveries
        ],
        "supervision_ratio": policy["supervision_ratio"] if policy else None,
    }


def bank_view(ledger):
    """主办银行判断合理融资所需的项目视图。"""
    idx = {
        "milestones": _index(ledger["milestones"]),
        "contracts": _index(ledger["contracts"]),
    }
    supervised = [a for a in ledger["accounts"] if a["kind"] == "supervised"][0]
    general = [a for a in ledger["accounts"] if a["kind"] == "general"][0]
    executed = [p for p in ledger["payments"] if p["status"] == "executed"]

    supervised_in = sum(
        r["supervised_amount"] for r in ledger["receipts"] if not r.get("off_account")
    )
    supervised_out = sum(
        p["amount"] for p in executed if p["account_id"] == supervised["id"]
    )
    milestone_caps = {m["id"]: m["budget_cap"] for m in ledger["milestones"]}
    milestone_paid = {
        m["id"]: _milestone_paid_total(ledger, m["id"])
        for m in ledger["milestones"]
    }
    certified_unpaid = sum(
        milestone_caps[m["id"]] - milestone_paid[m["id"]]
        for m in ledger["milestones"]
        if m["certified_by"]
    )
    uncertified_budget = sum(
        m["budget_cap"] for m in ledger["milestones"] if not m["certified_by"]
    )
    open_freezes = [f for f in ledger["freeze_reviews"] if f["result"] == "pending"]
    active_takeovers = [
        t for t in ledger["takeovers"]
        if not t.get("end_date") or t["end_date"] >= ledger["as_of"]
    ]
    return {
        "project": ledger["project"]["name"],
        "company": ledger["company"]["name"],
        "as_of": ledger["as_of"],
        "supervised_balance": supervised.get("closing_balance"),
        "general_balance": general.get("closing_balance"),
        "supervised_in_total": supervised_in,
        "supervised_out_total": supervised_out,
        "certified_unpaid_cap": certified_unpaid,
        "uncertified_budget": uncertified_budget,
        "open_freeze_count": len(open_freezes),
        "active_takeover_count": len(active_takeovers),
        "off_account_receipt_count": sum(
            1 for r in ledger["receipts"] if r.get("off_account")
        ),
    }


def trace(ledger, entry_kind, entry_id):
    """监管人员沿任一资金流找到项目、节点、审批与最终用途。"""
    if entry_kind == "payment":
        return _trace_payment(ledger, entry_id)
    if entry_kind == "receipt":
        return _trace_receipt(ledger, entry_id)
    if entry_kind == "unit":
        return _trace_unit(ledger, entry_id)
    raise LedgerError(f"不支持的追踪入口 {entry_kind}")


def _trace_payment(ledger, payment_id):
    payments = _index(ledger["payments"])
    p = payments.get(payment_id)
    if p is None:
        raise LedgerError(f"付款 {payment_id} 不存在")
    accounts = _index(ledger["accounts"])
    payees = _index(ledger["payees"])
    out = {
        "payment": p,
        "project": ledger["project"]["name"],
        "company": ledger["company"]["name"],
        "account": accounts[p["account_id"]]["name"],
        "payee": payees[p["payee_id"]]["name"],
        "approvals": [a for a in ledger["approvals"] if a["payment_id"] == p["id"]],
        "callbacks": [c for c in ledger["callbacks"] if c["payment_id"] == p["id"]],
        "freeze_reviews": [
            f for f in ledger["freeze_reviews"] if f.get("payment_id") == p["id"]
        ],
    }
    if p.get("milestone_id"):
        milestones = _index(ledger["milestones"])
        buildings = _index(ledger["buildings"])
        m = milestones[p["milestone_id"]]
        b = buildings[m["building_id"]]
        out["milestone"] = m
        out["building"] = b["name"]
        out["parcel"] = next(
            x["name"] for x in ledger["parcels"] if x["id"] == b["parcel_id"]
        )
    if p.get("obligation_id"):
        obligations = _index(ledger["obligations"])
        out["obligation"] = obligations[p["obligation_id"]]
    if p.get("contract_id"):
        contracts = _index(ledger["contracts"])
        out["contract"] = contracts[p["contract_id"]]
    return out


def _trace_receipt(ledger, receipt_id):
    receipts = _index(ledger["receipts"])
    r = receipts.get(receipt_id)
    if r is None:
        raise LedgerError(f"回款 {receipt_id} 不存在")
    accounts = _index(ledger["accounts"])
    out = {
        "receipt": r,
        "project": ledger["project"]["name"],
        "account": accounts[r["account_id"]]["name"],
        "freeze_reviews": [
            f for f in ledger["freeze_reviews"] if f.get("receipt_id") == r["id"]
        ],
        "corrections": [
            x for x in ledger["receipts"] if x.get("corrects") == r["id"]
        ],
    }
    if r.get("contract_id"):
        contracts = _index(ledger["contracts"])
        c = contracts[r["contract_id"]]
        units = _index(ledger["units"])
        buildings = _index(ledger["buildings"])
        unit = units[c["unit_id"]]
        out["contract"] = c
        out["unit"] = unit["name"]
        out["building"] = buildings[unit["building_id"]]["name"]
    return out


def _trace_unit(ledger, unit_id):
    units = _index(ledger["units"])
    u = units.get(unit_id)
    if u is None:
        raise LedgerError(f"房源 {unit_id} 不存在")
    buildings = _index(ledger["buildings"])
    b = buildings[u["building_id"]]
    contracts = [c for c in ledger["contracts"] if c["unit_id"] == unit_id]
    receipts = [
        r for r in ledger["receipts"]
        if r.get("contract_id") in {c["id"] for c in contracts}
    ]
    deliveries = [
        d for d in ledger["deliveries"]
        if d["contract_id"] in {c["id"] for c in contracts}
    ]
    return {
        "unit": u,
        "building": b["name"],
        "parcel": next(
            x["name"] for x in ledger["parcels"] if x["id"] == b["parcel_id"]
        ),
        "project": ledger["project"]["name"],
        "contracts": contracts,
        "receipts": receipts,
        "deliveries": deliveries,
        "milestones": [
            m for m in ledger["milestones"] if m["building_id"] == b["id"]
        ],
    }
