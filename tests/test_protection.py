import json
import unittest
from datetime import date
from pathlib import Path

from src.domain import load_domain
from src import protection as p

FIXTURE = Path("fixtures/domain.json")


def records():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def application(recs, app_id):
    return next(a for a in recs["applications"] if a["application_id"] == app_id)


class FixtureShapeTest(unittest.TestCase):
    def test_domain_still_loads(self):
        value = load_domain(FIXTURE)
        self.assertEqual(value["domain"], "drug-data-protection")
        self.assertGreaterEqual(value["version"], 2)

    def test_integrity_clean(self):
        problems = p.validate_integrity(records())
        self.assertEqual(problems, [])


class PointInTimeRegulationTest(unittest.TestCase):
    def test_regulation_in_force_on_date(self):
        recs = records()
        self.assertEqual(
            p.effective_regulation(recs, date(2025, 8, 15))["reg_id"], "REG-2024"
        )
        self.assertEqual(
            p.effective_regulation(recs, date(2026, 3, 1))["reg_id"], "REG-2026"
        )

    def test_application_does_not_see_future_intervals(self):
        """2025-08-15 的申请不能看到 2025-09-30 的恢复区间，更看不到 2026 换版。"""
        recs = records()
        c102 = next(c for c in recs["claims"] if c["claim_id"] == "C-102")
        interval = p.interval_on_date(c102, date(2025, 8, 15))
        self.assertEqual(interval["interval_id"], "C-102-I2")
        self.assertEqual(interval["status"], "suspended")
        self.assertIsNone(p.projected_expiry(c102, date(2025, 8, 15)))


class ThreeWayDispositionTest(unittest.TestCase):
    def test_accept_when_no_claim_exists(self):
        recs = records()
        result = p.replay_application(recs, application(recs, "A-2024-03"))
        self.assertEqual(result.outcome, p.ACCEPT)
        self.assertEqual(result.blocking_claim_ids, [])

    def test_wait_expiry_against_active_claim(self):
        recs = records()
        result = p.replay_application(recs, application(recs, "A-2024-07"))
        self.assertEqual(result.outcome, p.WAIT_EXPIRY)
        self.assertEqual(result.blocking_claim_ids, ["C-101"])
        # 1826 天 = 2024-06-30 + 5 年
        self.assertEqual(result.projected_expiries["C-101"], "2029-06-30")

    def test_accept_via_exception_window(self):
        recs = records()
        result = p.replay_application(recs, application(recs, "A-2024-11"))
        self.assertEqual(result.outcome, p.ACCEPT)
        self.assertEqual(result.invoked_exception_ids, ["X-01"])

    def test_defer_during_court_suspension(self):
        recs = records()
        result = p.replay_application(recs, application(recs, "A-2025-08"))
        self.assertEqual(result.outcome, p.DEFER)
        self.assertEqual(result.suspended_claim_ids, ["C-102"])
        self.assertEqual(result.reg_id, "REG-2024")

    def test_resume_after_suspension_extends_expiry_by_tolled_days(self):
        recs = records()
        before = p.replay_application(recs, application(recs, "A-2025-08"))
        after = p.replay_application(recs, application(recs, "A-2025-10"))
        self.assertEqual(before.outcome, p.DEFER)
        self.assertEqual(after.outcome, p.WAIT_EXPIRY)
        c102 = next(c for c in recs["claims"] if c["claim_id"] == "C-102")
        self.assertEqual(p.tolled_days(c102), 92)
        # 2557 天孤儿药独占期 + 92 天中止顺延
        self.assertEqual(
            p.projected_expiry(c102, date(2025, 10, 1)),
            date(2025, 2, 1) + __import__("datetime").timedelta(days=2557 + 92),
        )

    def test_new_regulation_applies_only_to_later_filings(self):
        recs = records()
        aug = p.replay_application(recs, application(recs, "A-2025-08"))
        apr = p.replay_application(recs, application(recs, "A-2026-04"))
        self.assertEqual(aug.reg_id, "REG-2024")
        self.assertEqual(apr.reg_id, "REG-2026")
        self.assertEqual(aug.outcome, p.DEFER)
        self.assertEqual(apr.outcome, p.WAIT_EXPIRY)


class EventsCreateIntervalsTest(unittest.TestCase):
    def test_events_never_overwrite_prior_intervals(self):
        recs = records()
        c102 = next(c for c in recs["claims"] if c["claim_id"] == "C-102")
        ids = [i["interval_id"] for i in c102["intervals"]]
        self.assertEqual(ids, ["C-102-I1", "C-102-I2", "C-102-I3", "C-102-I4"])
        # 每个区间引用各自当时的核定，旧核定原样保留
        dets = [i["determination_id"] for i in c102["intervals"]]
        self.assertEqual(
            dets,
            ["DET-2025-01", "DET-2025-02", "DET-2025-03", "DET-2026-01"],
        )

    def test_transfer_does_not_change_interval_status(self):
        """持有人变更只登记转让，不开新区间、不改中止状态。"""
        recs = records()
        tr = next(t for t in recs["transfers"] if t["transfer_id"] == "TR-1")
        c102 = next(c for c in recs["claims"] if c["claim_id"] == "C-102")
        interval = p.interval_on_date(c102, date.fromisoformat(tr["effective_date"]))
        self.assertEqual(interval["status"], "suspended")

    def test_withdrawn_submission_is_retained_not_deleted(self):
        recs = records()
        s101 = next(s for s in recs["submissions"] if s["submission_id"] == "S-101")
        s101a = next(s for s in recs["submissions"] if s["submission_id"] == "S-101a")
        self.assertEqual(s101["withdrawn_date"], "2024-05-25")
        self.assertEqual(s101a["supersedes"], "S-101")


class FrozenHistoryTest(unittest.TestCase):
    def test_all_frozen_reviews_match_filing_date_replay(self):
        problems = p.verify_frozen_reviews(records())
        self.assertEqual(problems, [])

    def test_tampering_a_frozen_review_is_detected(self):
        recs = records()
        review = next(
            r for r in recs["administrative_reviews"] if r["review_id"] == "ADM-2025-08"
        )
        review["outcome"] = "等待保护期届满"  # 用后来状态覆盖历史
        problems = p.verify_frozen_reviews(recs)
        self.assertTrue(any("ADM-2025-08" in msg for msg in problems))

    def test_cannot_recompute_old_application_from_latest_state(self):
        """同申请人不同申请日得出不同结论，且 8 月的暂缓永不因后来恢复而改变。"""
        recs = records()
        aug = p.replay_application(recs, application(recs, "A-2025-08"))
        oct_ = p.replay_application(recs, application(recs, "A-2025-10"))
        self.assertNotEqual(aug.outcome, oct_.outcome)
        recorded_aug = next(
            r for r in recs["administrative_reviews"] if r["review_id"] == "ADM-2025-08"
        )
        self.assertEqual(recorded_aug["outcome"], "暂缓")


class ViewsTest(unittest.TestCase):
    def test_applicant_view_hides_other_parties_study_data(self):
        recs = records()
        view = p.applicant_view(recs, "乙公司", date(2025, 8, 15))
        flat = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("D-01", flat)
        self.assertNotIn("AUTH-01", flat)
        self.assertNotIn("K-01", flat)
        # 仍能看到可披露的保护状态与本方待补事项
        self.assertTrue(any(i["status"] == "suspended" for i in view["disclosable_protection"]))
        self.assertTrue(any("授权" in item for item in view["pending_items"]))

    def test_reviewer_sees_conflicting_claims_with_basis(self):
        recs = records()
        conflicts = p.reviewer_conflict_view(recs, date(2025, 8, 15))
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["active_ingredient"], "示例酸A")
        holders = {c["holder"] for c in conflict["claims"]}
        self.assertEqual(holders, {"甲公司", "乙公司"})
        # 每条冲突主张都带当时的核定依据，供复核
        for c in conflict["claims"]:
            self.assertTrue(c["determination_id"].startswith("DET-"))


class DataSourceClassificationTest(unittest.TestCase):
    def test_three_kinds_of_data_sources_are_registered(self):
        recs = records()
        statuses = {
            src["status"]
            for pkg in recs["study_packages"]
            for src in pkg["data_sources"]
        }
        self.assertEqual(statuses, {"自有数据", "授权引用", "公开披露", "补充提交"})

    def test_authorized_reference_carries_authorization_chain(self):
        recs = records()
        d02 = next(
            src
            for pkg in recs["study_packages"]
            for src in pkg["data_sources"]
            if src["source_id"] == "D-02"
        )
        auth = next(
            a for a in recs["authorizations"]
            if a["authorization_id"] == d02["authorization_id"]
        )
        self.assertIsNone(auth["revoked_date"])
        self.assertIn("P-101", auth["scope"])


if __name__ == "__main__":
    unittest.main()
