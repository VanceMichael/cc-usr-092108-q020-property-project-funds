import copy
import json
import unittest
from pathlib import Path

from src.ledger import (
    LedgerError,
    apply_bank_callback,
    audit_hash,
    bank_view,
    buyer_view,
    contract_policy,
    trace,
    validate_ledger,
)

FIXTURE = Path("fixtures/ledger.json")


def load():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def mutation_check(ledger, mutate):
    """返回对副本执行 mutate 后的校验错误列表。"""
    copy_ledger = copy.deepcopy(ledger)
    mutate(copy_ledger)
    return validate_ledger(copy_ledger)


def expect_errors(ledger, mutate, *fragments):
    errors = mutation_check(ledger, mutate)
    self_ = unittest.TestCase()
    self_.assertTrue(errors, "本应校验失败，但通过了")
    joined = "\n".join(errors)
    for frag in fragments:
        self_.assertIn(frag, joined, f"错误信息应包含 {frag}，实际：{joined}")


class FixtureTest(unittest.TestCase):
    def test_fixture_is_valid(self):
        self.assertEqual(validate_ledger(load()), [])

    def test_required_top_level_keys(self):
        ledger = load()
        del ledger["payments"]
        self.assertTrue(validate_ledger(ledger))


class IndependentAccountTest(unittest.TestCase):
    def test_supervised_account_must_be_project_company(self):
        expect_errors(
            load(),
            lambda l: next(
                a for a in l["accounts"] if a["kind"] == "supervised"
            ).__setitem__("owner_company_id", "CO-HQ"),
            "独立账",
        )

    def test_exactly_one_supervised_account(self):
        def duplicate(l):
            extra = copy.deepcopy(next(
                a for a in l["accounts"] if a["kind"] == "supervised"))
            extra["id"] = "ACC-S2"
            l["accounts"].append(extra)
        expect_errors(load(), duplicate, "有且仅有一个监管账户")


class PaymentBasisTest(unittest.TestCase):
    def test_payment_must_reference_milestone(self):
        def drop(l):
            l["payments"][0]["milestone_id"] = None
        expect_errors(load(), drop, "必须对应工程节点")

    def test_payment_must_follow_certified_node(self):
        def point_to_uncertified(l):
            p = next(p for p in l["payments"] if p["id"] == "PM-07")
            p["milestone_id"] = "MS-202"  # 2 号楼竣工节点尚未核验
        expect_errors(load(), point_to_uncertified, "尚未核验")

    def test_payment_before_certification_rejected(self):
        def backdate(l):
            next(p for p in l["payments"] if p["id"] == "PM-01")[
                "date"] = "2024-01-01"
            for a in l["approvals"]:
                if a["payment_id"] == "PM-01":
                    a["date"] = "2023-12-30"
            for c in l["callbacks"]:
                if c["payment_id"] == "PM-01":
                    c["date"] = "2024-01-01"
        expect_errors(load(), backdate, "早于节点")

    def test_milestone_budget_cap_enforced(self):
        def overpay(l):
            next(p for p in l["payments"] if p["id"] == "PM-10")[
                "amount"] = 29_000_000  # MS-201 预算额度仅 2800 万
        expect_errors(load(), overpay, "超过预算额度")

    def test_obligation_due_date_enforced(self):
        def late(l):
            next(p for p in l["payments"] if p["id"] == "PM-03")[
                "date"] = "2024-12-31"
            for a in l["approvals"]:
                if a["payment_id"] == "PM-03":
                    a["date"] = "2024-12-30"
            for c in l["callbacks"]:
                if c["payment_id"] == "PM-03":
                    c["date"] = "2024-12-31"
        expect_errors(load(), late, "到期日")

    def test_obligation_not_paid_twice(self):
        def duplicate(l):
            p = copy.deepcopy(next(p for p in l["payments"] if p["id"] == "PM-03"))
            p["id"] = "PM-03B"
            p["date"] = "2024-06-13"
            for a in l["approvals"]:
                if a["payment_id"] == "PM-03":
                    a2 = copy.deepcopy(a)
                    a2["id"] = a["id"] + "B"
                    a2["payment_id"] = "PM-03B"
                    a2["date"] = "2024-06-13"
                    l["approvals"].append(a2)
            l["payments"].append(p)
        expect_errors(load(), duplicate, "重复付款")


class FreezeReviewTest(unittest.TestCase):
    def test_hq_transfer_triggers_freeze(self):
        def no_review(l):
            l["freeze_reviews"] = [
                f for f in l["freeze_reviews"] if f.get("payment_id") != "PM-06"
            ]
        expect_errors(load(), no_review, "未触发冻结复核")

    def test_rejected_freeze_blocks_payment(self):
        def force_execute(l):
            next(p for p in l["payments"] if p["id"] == "PM-06")[
                "status"] = "executed"
        expect_errors(load(), force_execute, "未拒绝")

    def test_off_account_receipt_triggers_freeze_and_recall(self):
        def no_review(l):
            l["freeze_reviews"] = [
                f for f in l["freeze_reviews"] if f.get("receipt_id") != "RC-012"
            ]
        expect_errors(load(), no_review, "账户外回款", "未触发冻结复核")

    def test_off_account_money_must_be_recalled_to_supervised(self):
        def no_correction(l):
            l["receipts"] = [r for r in l["receipts"] if r["id"] != "RC-013"]
        expect_errors(load(), no_correction, "纠正入账")

    def test_off_account_money_never_enters_project_accounts(self):
        def reroute(l):
            r = next(r for r in l["receipts"] if r["id"] == "RC-012")
            r["account_id"] = "ACC-S"
            r["general_amount"] = 50_000
        expect_errors(load(), reroute, "不得进入项目")


class ContractPolicyTest(unittest.TestCase):
    def test_presale_contract_keeps_old_60_ratio_after_2025_reform(self):
        # 2024 年签署的预售合同始终适用 P-2023（60%）
        ledger = load()
        policy = contract_policy(ledger, next(
            c for c in ledger["contracts"] if c["id"] == "CT-001"))
        self.assertEqual(policy["id"], "P-2023")
        self.assertEqual(policy["supervision_ratio"], 60)

    def test_finished_contract_uses_80_ratio(self):
        ledger = load()
        policy = contract_policy(ledger, next(
            c for c in ledger["contracts"] if c["id"] == "CT-002"))
        self.assertEqual(policy["id"], "P-2025")
        self.assertEqual(policy["supervision_ratio"], 80)

    def test_retroactive_ratio_change_fails(self):
        # 用新制度 80% 重算旧合同会造成拆分不匹配
        def retrofit(l):
            old = next(p for p in l["policies"] if p["id"] == "P-2023")
            old["supervision_ratio"] = 80  # 假设追溯改写
        expect_errors(load(), retrofit, "CT-001", "签署时制度")

    def test_finished_sale_requires_acceptance(self):
        def remove_acceptance(l):
            next(b for b in l["buildings"] if b["id"] == "BLD-1")[
                "acceptance"] = None
        expect_errors(load(), remove_acceptance, "尚未竣工验收")

    def test_finished_sale_requires_visible_listing(self):
        def hide_unit(l):
            next(u for u in l["units"] if u["id"] == "UNIT-101")[
                "visible_status"] = "unlisted"
        expect_errors(load(), hide_unit, "可见在售状态")

    def test_unaccepted_building_cannot_be_sold_as_finished(self):
        def fake_finished(l):
            # 2 号楼未竣工，把预售合同伪装成现房
            c = next(c for c in l["contracts"] if c["id"] == "CT-001")
            c["sale_kind"] = "finished"
            c["presale_approval"] = None
            c["sign_date"] = "2026-02-01"
            l["freeze_reviews"] = [
                f for f in l["freeze_reviews"]
                if f.get("receipt_id") != "RC-012"
            ]
            l["receipts"] = [
                r for r in l["receipts"]
                if r["id"] not in ("RC-012",)
            ]
        expect_errors(load(), fake_finished, "尚未竣工验收")

    def test_presale_needs_approval(self):
        def drop_approval(l):
            next(c for c in l["contracts"] if c["id"] == "CT-001")[
                "presale_approval"] = None
        expect_errors(load(), drop_approval, "预售审批")


class ApprovalAndCallbackTest(unittest.TestCase):
    def test_executed_payment_needs_three_roles(self):
        def drop_bank(l):
            l["approvals"] = [
                a for a in l["approvals"]
                if not (a["payment_id"] == "PM-01" and a["role"] == "bank")
            ]
        expect_errors(load(), drop_bank, "bank")

    def test_duplicate_callback_is_idempotent(self):
        ledger = load()
        before_balance = bank_view(ledger)["general_balance"]
        dup = {
            "id": "CB-DUP", "payment_id": "PM-01",
            "bank_ref": "BK-PAY-20240610-01", "date": "2024-06-12",
        }
        result = apply_bank_callback(ledger, dup)
        self.assertEqual(result["status"], "ignored_duplicate")
        self.assertEqual(validate_ledger(ledger), [])
        self.assertEqual(bank_view(ledger)["general_balance"], before_balance)

    def test_first_callback_applied(self):
        ledger = load()
        fresh = {
            "id": "CB-NEW", "payment_id": "PM-02",
            "bank_ref": "BK-PAY-20240905-99", "date": "2024-09-06",
        }
        self.assertEqual(apply_bank_callback(ledger, fresh)["status"], "applied")
        self.assertEqual(validate_ledger(ledger), [])


class RefundTest(unittest.TestCase):
    def test_refund_requires_rescinded_contract(self):
        def unrescind(l):
            next(c for c in l["contracts"] if c["id"] == "CT-003")[
                "status"] = "active"
        expect_errors(load(), unrescind, "未解除")

    def test_refund_cannot_exceed_paid_in(self):
        def inflate(l):
            next(p for p in l["payments"] if p["id"] == "PM-08")[
                "amount"] = 5_000_000
        expect_errors(load(), inflate, "实缴")


class TakeoverTest(unittest.TestCase):
    def test_frozen_takeover_blocks_all_payments(self):
        def add_payment_in_freeze(l):
            p = {
                "id": "PM-T", "date": "2026-08-01", "type": "milestone",
                "account_id": "ACC-G", "payee_id": "PAY-GC",
                "amount": 1_000_000, "status": "executed",
                "milestone_id": "MS-201", "purpose": "冻结期内违规付款",
            }
            l["payments"].append(p)
            for i, role in enumerate(("developer", "supervisor", "bank")):
                l["approvals"].append({
                    "id": f"AP-T{i}", "payment_id": "PM-T", "role": role,
                    "actor": "x", "decision": "approve",
                    "date": "2026-07-30",
                })
            l["callbacks"].append({
                "id": "CB-T", "payment_id": "PM-T",
                "bank_ref": "BK-T", "date": "2026-08-01",
                "status": "applied",
            })
        expect_errors(load(), add_payment_in_freeze, "冻结期内")

    def test_committee_only_takeover_requires_committee_approval(self):
        def drop_committee(l):
            l["approvals"] = [a for a in l["approvals"]
                              if not (a["payment_id"] == "PM-05"
                                      and a["role"] == "committee")]
        expect_errors(load(), drop_committee, "风险处置专班审批")


class BalanceTest(unittest.TestCase):
    def test_no_overdraft(self):
        def drain(l):
            # 先于任何回款执行一笔巨额付款
            p = next(p for p in l["payments"] if p["id"] == "PM-01")
            p["date"] = "2024-01-02"
            p["amount"] = 40_000_000
            for a in l["approvals"]:
                if a["payment_id"] == "PM-01":
                    a["date"] = "2024-01-01"
            for c in l["callbacks"]:
                if c["payment_id"] == "PM-01":
                    c["date"] = "2024-01-02"
        expect_errors(load(), drain, "透支")


class AuditChainTest(unittest.TestCase):
    def test_tampering_breaks_chain(self):
        def tamper(l):
            l["audit_events"][5]["action"] = "进度款被悄悄改为上划总部"
        expect_errors(load(), tamper, "哈希")

    def test_recomputed_chain_after_appending_event(self):
        ledger = load()
        prev = ledger["audit_events"][-1]["hash"]
        ev = {"id": "AU-NEW", "date": "2026-09-25",
              "actor": "监管人员", "action": "追加一条审计记录"}
        ev["prev_hash"] = prev
        ev["hash"] = audit_hash(prev, ev)
        ledger["audit_events"].append(ev)
        self.assertEqual(validate_ledger(ledger), [])


class ViewsTest(unittest.TestCase):
    def test_buyer_sees_only_delivery_evidence(self):
        view = buyer_view(load(), "CT-002", buyer="林舟")
        self.assertIn("acceptance", view)
        self.assertEqual(view["deliveries"][0]["evidence_ref"],
                         "YXF-2025-1220-101")
        self.assertNotIn("payments", view)
        self.assertNotIn("supervised_balance", view)

    def test_buyer_cannot_read_others_unit(self):
        with self.assertRaises(LedgerError):
            buyer_view(load(), "CT-002", buyer="周岚")

    def test_bank_view_summarizes_funding_basis(self):
        view = bank_view(load())
        self.assertEqual(view["supervised_balance"], 650_000)
        self.assertEqual(view["general_balance"], 200_000)
        self.assertEqual(view["open_freeze_count"], 1)
        self.assertEqual(view["active_takeover_count"], 1)
        self.assertEqual(view["off_account_receipt_count"], 1)
        # 已核验未付额度 = MS-102 1200 万 + MS-201 1800 万
        self.assertEqual(view["certified_unpaid_cap"], 30_000_000)

    def test_trace_payment_to_building_milestone_and_approvals(self):
        result = trace(load(), "payment", "PM-07")
        self.assertEqual(result["building"], "云栖苑 1 号楼")
        self.assertEqual(result["milestone"]["id"], "MS-102")
        roles = {a["role"] for a in result["approvals"]}
        self.assertEqual(roles, {"developer", "supervisor", "bank"})
        self.assertEqual(result["account"], "云栖苑预售资金监管专户")

    def test_trace_hq_transfer_shows_rejection_chain(self):
        result = trace(load(), "payment", "PM-06")
        self.assertEqual(result["payment"]["status"], "rejected")
        self.assertEqual(result["freeze_reviews"][0]["result"], "rejected")
        self.assertEqual(result["payee"], "云栖建设集团有限公司")

    def test_trace_off_account_receipt_to_correction(self):
        result = trace(load(), "receipt", "RC-012")
        self.assertTrue(result["receipt"]["off_account"])
        self.assertEqual(result["freeze_reviews"][0]["result"], "returned")
        self.assertEqual(result["corrections"][0]["id"], "RC-013")

    def test_trace_unit_covers_contracts_receipts_and_delivery(self):
        result = trace(load(), "unit", "UNIT-101")
        self.assertEqual(result["building"], "云栖苑 1 号楼")
        self.assertEqual(result["contracts"][0]["id"], "CT-002")
        self.assertTrue(result["receipts"])
        self.assertEqual(result["deliveries"][0]["evidence_ref"],
                         "YXF-2025-1220-101")


if __name__ == "__main__":
    unittest.main()
