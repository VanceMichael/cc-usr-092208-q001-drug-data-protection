import copy
import json
import unittest
from pathlib import Path

from src.domain import load_domain, validate_book

FIXTURE = Path("fixtures/domain.json")
SCHEMA = Path("contracts/domain.schema.json")


class ContractTest(unittest.TestCase):
    def test_fixture_conforms_to_json_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("未安装 jsonschema，跳过契约校验")
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        jsonschema.validate(data, schema)



class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.book = load_domain(FIXTURE)

    def test_fixture_matches_domain(self):
        self.assertEqual(self.book["domain"], "drug-data-protection")
        self.assertGreaterEqual(self.book["version"], 2)
        self.assertGreaterEqual(len(self.book["constraints"]), 6)

    # -- 当时有效的法规 ------------------------------------------------------

    def test_rule_in_force_is_point_in_time(self):
        self.assertEqual(self.book.rule_in_force("2024-12-31")["rule_id"], "R1-2020")
        self.assertEqual(self.book.rule_in_force("2025-01-01")["rule_id"], "R2-2025")

    def test_rule_change_is_not_retroactive(self):
        # C-2 于 2023 年依首版授予 7 年：届满日不因 2025 年规则延长至 12 年而改变
        segment = self.book.segment_on("C-2", "2023-03-01")
        self.assertEqual(segment["term_end"], "2030-03-01")
        # 修订版下新授予的儿童药主张才是 12 年
        self.assertEqual(self.book.segment_on("C-4", "2025-06-01")["term_end"], "2037-06-01")

    # -- 三态核定 ------------------------------------------------------------

    def test_wait_when_inside_exclusivity(self):
        verdict = self.book.evaluate("A-1", "2024-06-10")
        self.assertEqual(verdict["outcome"], "wait")
        self.assertEqual(verdict["wait_until"], "2028-06-01")
        self.assertEqual(verdict["basis"]["rule_id"], "R1-2020")
        self.assertEqual(verdict["basis"]["interval_ids"], ["I-1"])

    def test_suspend_on_open_conflict(self):
        verdict = self.book.evaluate("A-3", "2024-02-20")
        self.assertEqual(verdict["outcome"], "suspend")
        self.assertTrue(any(i["code"] == "claim_conflict" for i in verdict["pending_items"]))

    def test_conflict_resolution_does_not_rewrite_earlier_decision(self):
        # 同一申请 A-3：冲突窗口期暂缓；冲突驳回后变为等待届满；两条记录并存
        earlier = self.book.evaluate("A-3", "2024-02-20")
        later = self.book.evaluate("A-3", "2024-09-10")
        self.assertEqual(earlier["outcome"], "suspend")
        self.assertEqual(later["outcome"], "wait")
        history = self.book.history_for("A-3")
        self.assertEqual([d["determination_id"] for d in history], ["D-2", "D-6"])
        self.assertEqual(history[0]["outcome"], "suspend")  # 历史结论原样保留

    def test_suspend_on_defective_citation(self):
        verdict = self.book.evaluate("A-2", "2024-07-12")
        self.assertEqual(verdict["outcome"], "suspend")
        codes = {i["code"] for i in verdict["pending_items"]}
        self.assertEqual(codes, {"citation_authorization_expired", "citation_scope_mismatch"})

    def test_accept_under_legal_exception(self):
        verdict = self.book.evaluate("A-4", "2024-08-10")
        self.assertEqual(verdict["outcome"], "accept")
        self.assertEqual(verdict["basis"]["exception_ids"], ["EX-1"])

    def test_accept_after_term_expiry(self):
        verdict = self.book.evaluate("A-5", "2029-02-10")
        self.assertEqual(verdict["outcome"], "accept")
        self.assertEqual(self.book.is_protected("C-1", "2029-02-10"), False)

    def test_withdrawal_creates_surrender_segment_but_history_remains(self):
        # 撤回后不再阻断新申请……
        self.assertEqual(self.book.evaluate("A-7", "2024-05-15")["outcome"], "accept")
        self.assertEqual(self.book.is_protected("C-5", "2024-05-15"), False)
        # ……但授予区间 I-6 与放弃区间 I-7 都在，且届满日不变
        chain_ids = [i["interval_id"] for i in self.book.interval_chain("C-5")]
        self.assertEqual(chain_ids, ["I-6", "I-7"])
        self.assertTrue(all(i["term_end"] == "2028-10-01" for i in
                            self.book.interval_chain("C-5")))

    def test_court_narrows_scope_but_preserves_term(self):
        # 2024-05-10 后 IND-002 已被法院裁定移出保护范围
        self.assertNotIn("IND-002", self.book.segment_on("C-2", "2024-06-01")["indication_ids"])
        self.assertIn("IND-004", self.book.segment_on("C-2", "2024-06-01")["indication_ids"])
        self.assertEqual(self.book.segment_on("C-2", "2024-06-01")["term_end"], "2030-03-01")

    def test_transfer_changes_holder_only_in_new_interval(self):
        before = self.book.segment_on("C-2", "2024-08-31")
        after = self.book.segment_on("C-2", "2024-09-01")
        self.assertEqual(before["holder_id"], "H1")
        self.assertEqual(after["holder_id"], "H2")
        self.assertEqual(before["term_end"], after["term_end"])
        self.assertEqual(after["basis"]["transfer_id"], "T-1")

    def test_public_disclosure_does_not_lift_protection(self):
        # DS-2 境外数据 2022-05-01 已公开，保护不因此终止
        self.assertEqual(self.book.evaluate("A-1", "2024-06-10")["outcome"], "wait")

    # -- 所有存档核定必须能用当时规则重算复现 --------------------------------

    def test_every_recorded_determination_is_reproducible(self):
        for recorded in self.book.determinations():
            with self.subTest(determination=recorded["determination_id"]):
                verdict = self.book.evaluate(
                    recorded["application_id"], recorded["decided_on"]
                )
                self.assertEqual(verdict["outcome"], recorded["outcome"])
                self.assertEqual(verdict["wait_until"], recorded["wait_until"])
                self.assertEqual(verdict["basis"], recorded["basis"])
                self.assertEqual(
                    {i["code"] for i in verdict["pending_items"]},
                    {i["code"] for i in recorded["pending_items"]},
                )

    # -- 视图 ----------------------------------------------------------------

    def test_applicant_view_hides_source_detail(self):
        view = self.book.applicant_view("H4")
        self.assertTrue(view["my_files"])
        disclosed = {key for item in view["protection_scope"] for key in item}
        # 可披露保护范围：届满日/权利人/适应症/依据法规；不披露研究包来源明细
        self.assertIn("term_end", disclosed)
        self.assertNotIn("source_ids", disclosed)
        self.assertNotIn("data_sources", disclosed)

    def test_reviewer_view_shows_conflicts_and_chains(self):
        view = self.book.reviewer_view("2024-03-01")
        self.assertEqual(view["rule_in_force"], "R1-2020")
        self.assertIn("C-1", view["open_conflicts"])
        chain = [i["kind"] for i in view["chains"]["C-2"]]
        self.assertEqual(chain, ["grant", "scope_narrow", "holder_change"])

    # -- 只追加写入 ----------------------------------------------------------

    def test_determinations_are_append_only(self):
        before = len(self.book.determinations())
        self.book.append_determination("A-5", "2029-02-10", "RV-1", "AP-1", "D-EXTRA")
        self.assertEqual(len(self.book.determinations()), before + 1)
        with self.assertRaises(ValueError):
            self.book.append_determination("A-5", "2029-02-10", "RV-1", "AP-1", "D-1")

    def test_append_requires_valid_approver(self):
        with self.assertRaises(ValueError):
            self.book.append_determination("A-5", "2029-02-10", "RV-1", "RV-2", "D-BAD")


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.base = load_domain(FIXTURE)

    def _expect_error(self, mutate, fragment):
        broken = copy.deepcopy(dict(self.base))
        mutate(broken)
        with self.assertRaises(ValueError) as ctx:
            validate_book(broken)
        self.assertIn(fragment, str(ctx.exception))

    def test_reject_retroactive_rule(self):
        self._expect_error(
            lambda b: b["rule_versions"][1].__setitem__("retroactive", True),
            "溯及既往",
        )

    def test_reject_term_tampering_in_later_interval(self):
        def mutate(b):
            b["effect_intervals"][3]["term_end"] = "2032-03-01"  # 转让段偷改届满日
        self._expect_error(mutate, "改动了法定届满日")

    def test_reject_decision_under_wrong_rule(self):
        def mutate(b):
            b["determinations"][0]["basis"]["rule_id"] = "R2-2025"  # 2022年的核定钉新版规则
        self._expect_error(mutate, "当日有效法规")

    def test_reject_broken_interval_chain(self):
        def mutate(b):
            b["effect_intervals"][2]["supersedes"] = "I-999"
        self._expect_error(mutate, "接替目标不存在")

    def test_reject_cross_ingredient_interval(self):
        def mutate(b):
            b["effect_intervals"][0]["product_ids"] = ["P-W"]  # ING-C 混入 ING-A 区间
        self._expect_error(mutate, "不同活性成分")


if __name__ == "__main__":
    unittest.main()
