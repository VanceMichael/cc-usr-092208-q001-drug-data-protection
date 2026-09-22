"""按申请时点回放保护期核定。

核心原则：对一项后来申请的处置（受理 / 暂缓 / 等待保护期届满），
只能依据 *申请当日* 有效的法规版本、效力区间、数据来源核定结论与签批记录计算；
撤回、持有人变更、法院决定、规则换版都只产生新的效力区间，
任何已冻结的历史核定都不得被后来状态覆盖或改写。
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

ACCEPT = "受理"
DEFER = "暂缓"
WAIT_EXPIRY = "等待保护期届满"


def _d(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def _index(records: list[dict], key: str) -> dict[str, dict]:
    return {r[key]: r for r in records}


# ---------------------------------------------------------------------------
# 时点查询
# ---------------------------------------------------------------------------

def effective_regulation(records: dict, on_date: date) -> dict:
    """返回指定当日现行有效的法规版本（已废止的排除，取最新生效者）。"""
    candidates = [
        r for r in records["regulation_versions"]
        if _d(r["effective_date"]) <= on_date
        and (r["repeal_date"] is None or _d(r["repeal_date"]) > on_date)
    ]
    if not candidates:
        raise LookupError(f"{on_date} 无生效法规版本")
    return max(candidates, key=lambda r: _d(r["effective_date"]))


def interval_on_date(claim: dict, on_date: date) -> Optional[dict]:
    """返回主张在指定当日所处的效力区间（区间左闭右开）。"""
    for interval in claim["intervals"]:
        start = _d(interval["start_date"])
        end = _d(interval["end_date"])
        if start <= on_date and (end is None or on_date < end):
            return interval
    return None


def tolled_days(claim: dict, through: Optional[date] = None) -> int:
    """累计 claim 在 through 之前各 suspended 区间的暂停天数（暂停不计时）。"""
    total = 0
    for interval in claim["intervals"]:
        if interval["status"] != "suspended":
            continue
        start = _d(interval["start_date"])
        end = _d(interval["end_date"])
        if end is None:
            continue
        if through is not None and end > through:
            end = max(start, through)
        total += (end - start).days
    return total


def projected_expiry(claim: dict, on_date: date) -> Optional[date]:
    """按 on_date 已知的区间预测届满日；中止/暂停期间顺延，pending 不起算。

    只用 on_date 之前已经形成的区间，保证早来的申请看不到未来事件。
    """
    current = interval_on_date(claim, on_date)
    if current is None or current["status"] != "active":
        return None
    term_days = current["term_days"]
    start = _d(claim["start_date"])
    return start + timedelta(days=term_days + tolled_days(claim, on_date))


def exception_active(records: dict, exception_id: str, on_date: date) -> bool:
    exc = _index(records["exceptions"], "exception_id")[exception_id]
    return _d(exc["start_date"]) <= on_date <= _d(exc["end_date"])


# ---------------------------------------------------------------------------
# 核定回放
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    application_id: str
    filing_date: date
    outcome: str
    reg_id: str
    blocking_claim_ids: list[str]
    suspended_claim_ids: list[str]
    unconfirmed_claim_ids: list[str]
    invoked_exception_ids: list[str]
    projected_expiries: dict[str, str]

    def basis(self) -> dict:
        """供复核的核定依据快照（不含他方研究数据细节）。"""
        return {
            "application_id": self.application_id,
            "filing_date": self.filing_date.isoformat(),
            "outcome": self.outcome,
            "reg_id": self.reg_id,
            "blocking_claim_ids": self.blocking_claim_ids,
            "suspended_claim_ids": self.suspended_claim_ids,
            "unconfirmed_claim_ids": self.unconfirmed_claim_ids,
            "invoked_exception_ids": self.invoked_exception_ids,
            "projected_expiries": self.projected_expiries,
        }


def replay_application(records: dict, application: dict) -> ReplayResult:
    """完全按申请日的信息重放处置结论。"""
    filing_date = _d(application["filing_date"])
    reg = effective_regulation(records, filing_date)
    claims = _index(records["claims"], "claim_id")

    blocking, suspended, unconfirmed = [], [], []
    expiries: dict[str, str] = {}

    for claim_id in application.get("target_claim_ids", []):
        claim = claims[claim_id]
        interval = interval_on_date(claim, filing_date)
        if interval is None:
            continue
        if interval["status"] == "suspended":
            suspended.append(claim_id)
        elif interval["status"] == "active":
            blocking.append(claim_id)
            expiry = projected_expiry(claim, filing_date)
            if expiry:
                expiries[claim_id] = expiry.isoformat()

    for claim_id in application.get("related_unconfirmed_claim_ids", []):
        claim = claims[claim_id]
        interval = interval_on_date(claim, filing_date)
        if interval is not None and interval["status"] == "pending":
            # 他人尚未确认的主张构成权利冲突，暂缓；本方自己的未确认主张仅作提示。
            if claim["holder"] != application.get("applicant_name"):
                unconfirmed.append(claim_id)

    invoked = []
    exc_id = application.get("invoke_exception_id")
    if exc_id and exception_active(records, exc_id, filing_date):
        invoked.append(exc_id)

    if invoked:
        outcome = ACCEPT
    elif suspended or unconfirmed:
        # 法院中止使届满日无法计算，或同活性成分上存在他人未确认的冲突主张。
        outcome = DEFER
    elif blocking:
        outcome = WAIT_EXPIRY
    else:
        outcome = ACCEPT

    return ReplayResult(
        application_id=application["application_id"],
        filing_date=filing_date,
        outcome=outcome,
        reg_id=reg["reg_id"],
        blocking_claim_ids=blocking,
        suspended_claim_ids=suspended,
        unconfirmed_claim_ids=unconfirmed,
        invoked_exception_ids=invoked,
        projected_expiries=expiries,
    )


# ---------------------------------------------------------------------------
# 历史决定不可覆盖
# ---------------------------------------------------------------------------

def verify_frozen_reviews(records: dict) -> list[str]:
    """逐条复核已冻结的受理复核记录与申请日回放结果一致；不一致即被覆盖的证据。"""
    reviews = _index(records["administrative_reviews"], "review_id")
    problems: list[str] = []
    for application in records["applications"]:
        review = reviews[application["review_determination_id"]]
        if not review.get("frozen_record"):
            problems.append(f"{review['review_id']} 未标记 frozen_record")
            continue
        replay = replay_application(records, application)
        if review["outcome"] != replay.outcome:
            problems.append(
                f"{review['review_id']} 冻结结论 {review['outcome']} 与申请日回放 {replay.outcome} 不一致"
            )
        if review["reg_id"] != replay.reg_id:
            problems.append(
                f"{review['review_id']} 记载法规 {review['reg_id']} 与申请日有效版本 {replay.reg_id} 不一致"
            )
        recorded = {b["claim_id"]: b["status"] for b in review.get("basis_claim_intervals", [])}
        replayed = {cid: "suspended" for cid in replay.suspended_claim_ids}
        replayed.update({cid: "active" for cid in replay.blocking_claim_ids})
        if recorded != replayed:
            problems.append(
                f"{review['review_id']} 依据区间 {recorded} 与回放 {replayed} 不一致"
            )
    return problems


def verify_determinations_immutable(records: dict) -> list[str]:
    """区间引用的每条核定都必须存在且已冻结；同一核定不得被二次改写。"""
    problems: list[str] = []
    determinations = _index(records["determinations"], "determination_id")
    for claim in records["claims"]:
        for interval in claim["intervals"]:
            det = determinations.get(interval["determination_id"])
            if det is None:
                problems.append(f"{claim['claim_id']} 区间引用了不存在的核定 {interval['determination_id']}")
            elif not det.get("frozen_record"):
                problems.append(f"核定 {det['determination_id']} 未冻结")
    return problems


# ---------------------------------------------------------------------------
# 双视图
# ---------------------------------------------------------------------------

def applicant_view(records: dict, applicant_name: str, on_date: date) -> dict:
    """申请人视图：仅可披露的保护范围、预测届满日与本方待补事项。

    不返回他方研究包、数据来源、授权文件等内容；冲突的未确认主张只做概括披露。
    """
    claims = records["claims"]
    visible: list[dict] = []
    own_pending: list[str] = []
    other_unconfirmed: list[str] = []

    for claim in claims:
        interval = interval_on_date(claim, on_date)
        if interval is None:
            continue
        entry = {
            "product_id": claim["product_id"],
            "status": interval["status"],
            "reg_id": interval["reg_id"],
            "applicable_reg_on_date": effective_regulation(records, on_date)["reg_id"],
        }
        expiry = projected_expiry(claim, on_date)
        if expiry:
            entry["projected_expiry"] = expiry.isoformat()

        if claim["holder"] == applicant_name:
            if claim.get("disclosable"):
                visible.append(entry)
            if interval["status"] == "pending":
                # 申请人版待补事项隐去他方授权链、数据来源等内部编号。
                own_pending.extend(
                    claim.get("applicant_pending_items", claim.get("pending_items", []))
                )
        elif interval["status"] == "pending":
            other_unconfirmed.append(
                f"同一活性成分上存在持有的尚未确认的独占期主张（产品 {claim['product_id']}）"
            )
        elif claim.get("disclosable"):
            visible.append(entry)

    return {
        "as_of": on_date.isoformat(),
        "applicant": applicant_name,
        "disclosable_protection": visible,
        "pending_items": own_pending,
        "notices": other_unconfirmed,
    }


def reviewer_conflict_view(records: dict, on_date: date) -> list[dict]:
    """审评人员视图：找出同一活性成分上当日相互冲突的主张及各自核定依据。"""
    by_ingredient: dict[str, list[dict]] = {}
    for product in records["products"]:
        by_ingredient.setdefault(product["active_ingredient"], []).append(product)

    claims = _index(records["claims"], "claim_id")
    conflicts: list[dict] = []
    for ingredient, products in by_ingredient.items():
        product_ids = {p["product_id"] for p in products}
        live = []
        for claim in records["claims"]:
            if claim["product_id"] not in product_ids:
                continue
            interval = interval_on_date(claim, on_date)
            if interval is not None:
                live.append((claim, interval))
        holders = {c["holder"] for c, _ in live}
        if len(live) > 1 and len(holders) > 1:
            conflicts.append({
                "active_ingredient": ingredient,
                "as_of": on_date.isoformat(),
                "claims": [
                    {
                        "claim_id": c["claim_id"],
                        "holder": c["holder"],
                        "product_id": c["product_id"],
                        "status": i["status"],
                        "determination_id": i["determination_id"],
                        "pending_items": c.get("pending_items", []),
                    }
                    for c, i in live
                ],
            })
    return conflicts


# ---------------------------------------------------------------------------
# 引用完整性与区间连续性
# ---------------------------------------------------------------------------

def validate_integrity(records: dict) -> list[str]:
    problems: list[str] = []
    ids: dict[str, set] = {
        "reg": {r["reg_id"] for r in records["regulation_versions"]},
        "product": {p["product_id"] for p in records["products"]},
        "claim": {c["claim_id"] for c in records["claims"]},
        "submission": {s["submission_id"] for s in records["submissions"]},
        "determination": {d["determination_id"] for d in records["determinations"]},
        "exception": {e["exception_id"] for e in records["exceptions"]},
        "package": {p["package_id"] for p in records["study_packages"]},
        "authorization": {a["authorization_id"] for a in records["authorizations"]},
        "transfer": {t["transfer_id"] for t in records["transfers"]},
    }

    for claim in records["claims"]:
        if claim["product_id"] not in ids["product"]:
            problems.append(f"{claim['claim_id']} 指向不存在的产品")
        if claim["submission_id"] not in ids["submission"]:
            problems.append(f"{claim['claim_id']} 指向不存在的提交")
        if claim["initial_reg_id"] not in ids["reg"]:
            problems.append(f"{claim['claim_id']} 初始法规不存在")
        intervals = sorted(claim["intervals"], key=lambda i: i["start_date"])
        for prev, nxt in zip(intervals, intervals[1:]):
            if prev["end_date"] != nxt["start_date"]:
                problems.append(
                    f"{claim['claim_id']} 区间不连续：{prev['interval_id']} 止于 {prev['end_date']}，"
                    f"{nxt['interval_id']} 始于 {nxt['start_date']}"
                )
        if intervals and intervals[-1]["end_date"] is not None:
            problems.append(f"{claim['claim_id']} 最后一个区间缺少开放结束日")

    for sub in records["submissions"]:
        if sub["product_id"] not in ids["product"]:
            problems.append(f"{sub['submission_id']} 指向不存在的产品")
        if sub["supersedes"] and sub["supersedes"] not in ids["submission"]:
            problems.append(f"{sub['submission_id']} 取代了不存在的提交")
        for pkg in sub.get("package_ids", []):
            if pkg not in ids["package"]:
                problems.append(f"{sub['submission_id']} 引用了不存在的研究包 {pkg}")

    for package in records["study_packages"]:
        if package["product_id"] not in ids["product"]:
            problems.append(f"{package['package_id']} 指向不存在的产品")
        for source in package["data_sources"]:
            auth = source.get("authorization_id")
            if auth and auth not in ids["authorization"]:
                problems.append(f"{package['package_id']}.{source['source_id']} 引用了不存在的授权 {auth}")

    for exc in records["exceptions"]:
        if exc["claim_id"] not in ids["claim"]:
            problems.append(f"{exc['exception_id']} 指向不存在的主张")
        if exc["determination_id"] not in ids["determination"]:
            problems.append(f"{exc['exception_id']} 指向不存在的核定")

    for event in records["events"]:
        if "claim_id" in event and event["claim_id"] not in ids["claim"]:
            problems.append(f"{event['event_id']} 指向不存在的主张")
        if "submission_id" in event and event["submission_id"] not in ids["submission"]:
            problems.append(f"{event['event_id']} 指向不存在的提交")
        if "transfer_id" in event and event["transfer_id"] not in ids["transfer"]:
            problems.append(f"{event['event_id']} 指向不存在的转让")
        det_id = event.get("determination_id")
        if det_id and det_id not in ids["determination"]:
            problems.append(f"{event['event_id']} 指向不存在的核定")

    reviews = {r["review_id"]: r for r in records["administrative_reviews"]}
    for application in records["applications"]:
        if application["product_id"] not in ids["product"]:
            problems.append(f"{application['application_id']} 指向不存在的产品")
        if application["review_determination_id"] not in reviews:
            problems.append(f"{application['application_id']} 缺少复核记录")
        for claim_id in application.get("target_claim_ids", []):
            if claim_id not in ids["claim"]:
                problems.append(f"{application['application_id']} 指向不存在的主张")
    for review in records["administrative_reviews"]:
        if review["reg_id"] not in ids["reg"]:
            problems.append(f"{review['review_id']} 引用了不存在的法规")

    problems.extend(verify_determinations_immutable(records))
    return problems
