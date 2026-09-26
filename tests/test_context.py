import copy
import unittest
from pathlib import Path

from src.context import load_context
from src.domain import (
    DomainError,
    audit_years,
    buyer_delivery_view,
    financing_view,
    load_domain,
    trace_fund,
    validate,
)

FIXTURE = Path("fixtures/context.json")


def raw():
    import json
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FixtureTest(unittest.TestCase):
    def test_load_context_still_works(self):
        value = load_context(FIXTURE)
        self.assertEqual(value["domain"], "property-project-funds")
        self.assertGreaterEqual(value["version"], 2)

    def test_fixture_passes_all_invariants(self):
        self.assertEqual(validate(raw()), [])


class ProjectStructureTest(unittest.TestCase):
    def test_project_company_independent_ledger(self):
        data = raw()
        project = data["projects"][0]
        company = next(a for a in data["actors"] if a["id"] == project["company_id"])
        self.assertEqual(company["independent_ledger_id"], project["ledger_id"])
        # 项目公司账户全部挂在项目下，总部账户不挂项目
        project_accounts = [a for a in data["accounts"] if a.get("project_id") == project["id"]]
        self.assertTrue({a["holder_id"] for a in project_accounts} <= {project["company_id"]})
        self.assertTrue(all(a["holder_id"] != "HQ-001" for a in project_accounts))

    def test_buildings_map_to_parcels_and_nodes_sum_to_one(self):
        data = raw()
        for project in data["projects"]:
            parcels = {p["id"] for p in project["parcels"]}
            for b in project["buildings"]:
                self.assertIn(b["parcel_id"], parcels)
        weights = {}
        for n in data["construction_nodes"]:
            weights.setdefault(n["building_id"], 0.0)
            weights[n["building_id"]] += n["weight"]
        for total in weights.values():
            self.assertAlmostEqual(total, 1.0)

    def test_broken_reference_is_rejected(self):
        data = raw()
        data["units"][0]["building_id"] = "B-X"
        self.assertTrue(validate(data))


class PaymentBasisTest(unittest.TestCase):
    def test_node_payment_traces_to_building_node_approval(self):
        chain = trace_fund(raw(), "E-1101")
        self.assertEqual(chain["project"]["id"], "PRJ-001")
        self.assertEqual(chain["parcel"]["id"], "P-01")
        self.assertEqual(chain["building"]["id"], "B-1")
        self.assertEqual(chain["node"]["name"], "主体结构封顶")
        self.assertEqual(chain["verification"]["id"], "V-B1-02")
        self.assertEqual(chain["approval"]["id"], "A-001")
        self.assertEqual(chain["approval"]["status"], "approved")
        # 多方审批：项目公司、监理、住建、银行
        self.assertEqual(len(chain["approval"]["parties"]), 4)

    def test_payment_before_node_verification_rejected(self):
        data = raw()
        entry = next(e for e in data["ledger_entries"] if e["id"] == "E-1101")
        entry["value_date"] = "2023-08-01"  # 早于 V-B1-02 核验日 2023-09-08
        errors = validate(data)
        self.assertTrue(any("付款早于节点核验" in m for m in errors))

    def test_payment_building_mismatch_rejected(self):
        data = raw()
        entry = next(e for e in data["ledger_entries"] if e["id"] == "E-1101")
        entry["building_id"] = "B-2"
        errors = validate(data)
        self.assertTrue(any("楼栋与核验节点不一致" in m for m in errors))

    def test_statutory_obligation_payment(self):
        chain = trace_fund(raw(), "E-1104")
        self.assertIn("法定义务", chain["final_use"])
        self.assertEqual(chain["approval"]["id"], "A-004")

    def test_node_payout_cannot_exceed_supervised_quota(self):
        data = raw()
        b1 = next(b for p in data["projects"] for b in p["buildings"] if b["id"] == "B-1")
        b1["supervised_quota_cny"] = 100  # 已拨付 400 万 + 接管期 200 万
        errors = validate(data)
        self.assertTrue(any("超过监管额度" in m for m in errors))


class FreezeReviewTest(unittest.TestCase):
    def test_hq_transfer_is_blocked_and_frozen(self):
        data = raw()
        a003 = next(a for a in data["approvals"] if a["id"] == "A-003")
        self.assertEqual(a003["status"], "rejected")
        blocked = next(b for b in data["blocked_payments"] if b["id"] == "BP-001")
        self.assertEqual(blocked["freeze_review_id"], "FR-001")
        fr = next(f for f in data["freeze_reviews"] if f["id"] == "FR-001")
        self.assertEqual(fr["outcome"], "blocked_and_retained")

    def test_approving_hq_transfer_rejected_by_validator(self):
        data = raw()
        a003 = next(a for a in data["approvals"] if a["id"] == "A-003")
        a003["status"] = "approved"
        errors = validate(data)
        self.assertTrue(any("总部调拨" in m for m in errors))

    def test_related_party_payment_requires_freeze_review(self):
        data = raw()
        entry = next(e for e in data["ledger_entries"] if e["id"] == "E-1102")
        entry.pop("freeze_review_id")
        errors = validate(data)
        self.assertTrue(any("关联方付款未挂冻结复核" in m for m in errors))

    def test_related_party_released_after_enhanced_review(self):
        data = raw()
        fr = next(f for f in data["freeze_reviews"] if f["id"] == "FR-002")
        self.assertEqual(fr["trigger"], "related_party_transaction")
        self.assertEqual(fr["outcome"], "released_after_enhanced_review")
        self.assertIn("监理重新现场核验节点", fr["conditions"])

    def test_off_account_repayment_clawed_back_to_escrow(self):
        data = raw()
        fr = next(f for f in data["freeze_reviews"] if f["id"] == "FR-003")
        self.assertEqual(fr["trigger"], "off_account_repayment")
        clawback = next(e for e in data["ledger_entries"]
                        if e.get("freeze_review_id") == "FR-003")
        self.assertEqual(clawback["account_id"], "ACC-ESC-01")
        self.assertEqual(clawback["direction"], "in")

    def test_buyer_receipt_into_hq_account_is_off_account_violation(self):
        data = raw()
        entry = next(e for e in data["ledger_entries"] if e["id"] == "E-1001")
        entry["account_id"] = "ACC-HQ-01"
        errors = validate(data)
        self.assertTrue(any("账户外回款" in m or "非项目公司账户" in m for m in errors))


class SaleAndDeliveryTest(unittest.TestCase):
    def test_existing_contract_bound_to_completion_and_visible_listing(self):
        view = buyer_delivery_view(raw(), "BY-301", "U-301")
        kinds = {d["kind"] for d in view["evidence"]}
        self.assertEqual(kinds, {"completion_filing", "unit_acceptance", "handover_keys"})
        self.assertEqual(view["visible_listing"]["status"], "existing_visible")

    def test_uncompleted_building_cannot_be_packaged_as_existing(self):
        data = raw()
        # 后台直接拦截：5 号楼无竣工备案
        bp = next(b for b in data["blocked_payments"] if b["id"] == "BP-002")
        b5 = next(b for p in data["projects"] for b in p["buildings"] if b["id"] == "B-5")
        self.assertIsNone(b5["completion_filing_id"])
        self.assertEqual(bp["blocked_by"], "ORG-HC")
        # 若伪造现房合同，校验必须报错
        forged = copy.deepcopy(data["contracts"][3])
        forged["id"] = "C-FAKE"
        forged["unit_id"] = "U-501"
        forged["buyer_id"] = "BY-301"
        forged["signed_at"] = "2025-08-21"
        data["contracts"].append(forged)
        errors = validate(data)
        self.assertTrue(any("非现房楼栋" in m or "竣工" in m for m in errors))

    def test_presale_receipts_must_enter_escrow(self):
        data = raw()
        entry = next(e for e in data["ledger_entries"] if e["id"] == "E-1002")
        entry["account_id"] = "ACC-SET-02"
        errors = validate(data)
        self.assertTrue(any("未进入预售监管账户" in m for m in errors))

    def test_existing_funds_settle_on_delivery_account(self):
        data = raw()
        entry = next(e for e in data["ledger_entries"] if e["id"] == "E-2001")
        self.assertEqual(entry["account_id"], "ACC-NEW-01")


class ContractRuleLockTest(unittest.TestCase):
    def test_presale_contract_keeps_rule_version_at_signing(self):
        data = raw()
        old = next(c for c in data["contracts"] if c["id"] == "C-201")
        self.assertEqual(old["fund_rule_regime_id"], "REG-2020")
        chain = trace_fund(data, "E-1005")
        self.assertEqual(chain["contract"]["regime_at_signing"], "REG-2020")

    def test_locking_unsigned_future_regime_rejected(self):
        data = raw()
        c201 = next(c for c in data["contracts"] if c["id"] == "C-201")
        c201["fund_rule_regime_id"] = "REG-2024"  # 2024 规则对 2023 年合同未生效
        errors = validate(data)
        self.assertTrue(any("尚未生效的制度" in m for m in errors))

    def test_policy_change_is_never_retroactive(self):
        data = raw()
        for pc in data["policy_changes"]:
            self.assertFalse(pc["retroactive"])


class RefundTest(unittest.TestCase):
    def test_refund_returns_to_original_source(self):
        chain = trace_fund(raw(), "E-1106")
        self.assertEqual(chain["approval"]["id"], "A-005")
        data = raw()
        refund = next(e for e in data["ledger_entries"] if e["id"] == "E-1106")
        source = next(e for e in data["ledger_entries"] if e["id"] == refund["original_receipt_id"])
        self.assertEqual(refund["refund_target_source_id"], source["funds_source_id"])
        self.assertLessEqual(refund["amount_cny"], source["amount_cny"])

    def test_refund_to_different_source_rejected(self):
        data = raw()
        refund = next(e for e in data["ledger_entries"] if e["id"] == "E-1106")
        refund["refund_target_source_id"] = "SRC-OTHER"
        errors = validate(data)
        self.assertTrue(any("未原路退回" in m for m in errors))


class TakeoverTest(unittest.TestCase):
    def test_post_takeover_payout_includes_takeover_party(self):
        data = raw()
        chain = trace_fund(data, "E-1105")  # 2025-08-15，接管宣布于 2025-06-18
        parties = {p["org"] for p in chain["approval"]["parties"]}
        self.assertIn("江州城市建设投资集团有限公司（风险接管主体）", parties)

    def test_pre_takeover_payout_does_not_require_takeover_party(self):
        chain = trace_fund(raw(), "E-1104")  # 2025-04-12，接管前
        self.assertEqual(chain["approval"]["id"], "A-004")

    def test_takeover_payout_without_takeover_party_rejected(self):
        data = raw()
        a006 = next(a for a in data["approvals"] if a["id"] == "A-006")
        a006["parties"] = [p for p in a006["parties"] if p["org_id"] != "TO-001"]
        errors = validate(data)
        self.assertTrue(any("接管" in m and "审批缺少接管主体" in m for m in errors))


class BankCallbackTest(unittest.TestCase):
    def test_duplicate_callback_idempotent_single_payout(self):
        data = raw()
        cbs = [c for c in data["bank_callbacks"] if c["idempotency_key"] == "IDP-A-001"]
        self.assertEqual(len(cbs), 2)  # 重复回调确实到达
        payouts = [e for e in data["ledger_entries"]
                   if e.get("idempotency_key") == "IDP-A-001"]
        self.assertEqual(len(payouts), 1)  # 只入账一次

    def test_second_payout_under_same_key_rejected(self):
        data = raw()
        dup = copy.deepcopy(next(e for e in data["ledger_entries"] if e["id"] == "E-1101"))
        dup["id"] = "E-1101-DUP"
        dup["value_date"] = "2023-09-22"
        data["ledger_entries"].append(dup)
        errors = validate(data)
        self.assertTrue(any("重复回调必须只入账一次" in m for m in errors))


class MultiPartyApprovalTest(unittest.TestCase):
    def test_single_party_approval_rejected(self):
        data = raw()
        a001 = next(a for a in data["approvals"] if a["id"] == "A-001")
        a001["parties"] = [a001["parties"][0]]
        errors = validate(data)
        self.assertTrue(any("多方审批不足" in m for m in errors))

    def test_decisions_after_finalization_rejected(self):
        data = raw()
        a001 = next(a for a in data["approvals"] if a["id"] == "A-001")
        a001["parties"][-1]["decided_at"] = "2023-10-01"
        errors = validate(data)
        self.assertTrue(any("晚于终审时间" in m for m in errors))


class AuditTest(unittest.TestCase):
    def test_multi_year_audit_structure(self):
        years = audit_years(raw())["years"]
        self.assertEqual([y["year"] for y in years], [2023, 2024, 2025])
        sealed_2023 = next(y for y in years if y["year"] == 2023)
        self.assertTrue(sealed_2023["sealed"])
        self.assertEqual(sealed_2023["entry_count"], 6)

    def test_sealed_period_entries_unique(self):
        data = raw()
        data["audit"]["sealed_periods"][0]["entry_ids"].append("E-1101")
        errors = validate(data)
        self.assertTrue(any("封存台账分录重复" in m for m in errors))


class FinancingViewTest(unittest.TestCase):
    def test_bank_sees_quota_headroom_per_building(self):
        rows = {r["building_id"]: r for r in financing_view(raw())}
        self.assertEqual(rows["B-1"]["verified_node_paid_cny"], 6000000)
        self.assertEqual(rows["B-1"]["quota_headroom_cny"], 3000000)
        self.assertIn("额度余位", rows["B-1"]["bank_lending_principle"])

    def test_existing_building_uses_settlement_principle(self):
        rows = {r["building_id"]: r for r in financing_view(raw())}
        self.assertTrue(rows["B-3"]["completion_filed"])
        self.assertIn("现房", rows["B-3"]["bank_lending_principle"])

    def test_construction_only_building_no_presale_financing(self):
        rows = {r["building_id"]: r for r in financing_view(raw())}
        self.assertEqual(rows["B-5"]["presale_receipts_cny"], 0)
        self.assertIn("工程节点", rows["B-5"]["bank_lending_principle"])


class BuyerPermissionTest(unittest.TestCase):
    def test_buyer_sees_only_own_house(self):
        view = buyer_delivery_view(raw(), "BY-401", "U-401")
        self.assertEqual(view["contract_id"], "C-401")
        self.assertEqual(view["delivery_status"], "delivered")

    def test_buyer_cannot_query_other_house(self):
        with self.assertRaises(PermissionError):
            buyer_delivery_view(raw(), "BY-301", "U-401")

    def test_undelivered_presale_buyer_sees_status_without_others_data(self):
        view = buyer_delivery_view(raw(), "BY-101", "U-101")
        self.assertEqual(view["delivery_status"], "active")
        self.assertIsInstance(view["evidence"], str)


class InternalTransferTest(unittest.TestCase):
    def test_project_internal_transfer_pair_consistent(self):
        data = raw()
        pair = [e for e in data["ledger_entries"] if e.get("transfer_pair_id") == "TP-001"]
        self.assertEqual(len(pair), 2)
        self.assertEqual({e["direction"] for e in pair}, {"in", "out"})
        self.assertEqual(len({e["amount_cny"] for e in pair}), 1)
        self.assertEqual(len({e["value_date"] for e in pair}), 1)

    def test_unbalanced_transfer_pair_rejected(self):
        data = raw()
        entry = next(e for e in data["ledger_entries"] if e["id"] == "E-1301")
        entry["amount_cny"] = 1
        errors = validate(data)
        self.assertTrue(any("项目内划转" in m for m in errors))


class LoadFailureTest(unittest.TestCase):
    def test_invalid_fixture_raises_domain_error(self):
        import json
        import tempfile
        data = raw()
        data["projects"][0]["company_id"] = "NO-SUCH"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
            path = Path(f.name)
        with self.assertRaises(DomainError):
            load_domain(path)


if __name__ == "__main__":
    unittest.main()
