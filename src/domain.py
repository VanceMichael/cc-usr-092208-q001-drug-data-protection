"""读取、校验并核定药物试验数据保护期共享资料。

设计原则：
- 只追加：撤回、持有人变更、法院裁定、规则换版都形成新的效力区间或事件，
  历史区间与历史核定永久保留，任何代码路径都不提供改写它们的方法。
- 按当时有效规则复核：任何结论都钉住作出当日有效的法规版本，规则换版
  不溯及既往；届满日按授予区间所依据的规则重算。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# 基础读取
# ---------------------------------------------------------------------------

_REQUIRED = {
    "domain",
    "version",
    "sample_id",
    "actors",
    "facts",
    "constraints",
    "rule_versions",
    "staff",
    "holders",
    "active_ingredients",
    "indications",
    "products",
    "study_packages",
    "submissions",
    "claims",
    "legal_exceptions",
    "transfers",
    "court_decisions",
    "events",
    "effect_intervals",
    "applications",
    "determinations",
}

_TERM_PROVISION = {
    "data_protection": "data_protection_years",
    "orphan_exclusivity": "orphan_exclusivity_years",
    "pediatric_exclusivity": "pediatric_exclusivity_years",
}


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _add_years(value: str, years: int) -> str:
    start = _parse_date(value)
    try:
        return start.replace(year=start.year + years).isoformat()
    except ValueError:  # 2月29日等
        return start.replace(year=start.year + years, day=28).isoformat()


class RecordBook(dict):
    """带核定与复核能力的只追加资料册。映射接口保持与旧资料兼容。"""

    # -- 索引 ----------------------------------------------------------------

    def _by(self, key: str, id_field: str) -> dict[str, dict]:
        return {item[id_field]: item for item in self[key]}

    def rules(self) -> dict[str, dict]:
        return self._by("rule_versions", "rule_id")

    def staff(self) -> dict[str, dict]:
        return self._by("staff", "staff_id")

    def holders(self) -> dict[str, dict]:
        return self._by("holders", "holder_id")

    def ingredients(self) -> dict[str, dict]:
        return self._by("active_ingredients", "ingredient_code")

    def indications(self) -> dict[str, dict]:
        return self._by("indications", "indication_id")

    def products(self) -> dict[str, dict]:
        return self._by("products", "product_id")

    def packages(self) -> dict[str, dict]:
        return self._by("study_packages", "package_id")

    def submissions(self) -> dict[str, dict]:
        return self._by("submissions", "submission_id")

    def claims(self) -> dict[str, dict]:
        return self._by("claims", "claim_id")

    def exceptions(self) -> dict[str, dict]:
        return self._by("legal_exceptions", "exception_id")

    def transfers(self) -> dict[str, dict]:
        return self._by("transfers", "transfer_id")

    def court_decisions(self) -> dict[str, dict]:
        return self._by("court_decisions", "decision_id")

    def applications(self) -> dict[str, dict]:
        return self._by("applications", "application_id")

    def determinations(self) -> list[dict]:
        return self["determinations"]

    def data_sources(self) -> dict[str, dict]:
        indexed = {}
        for package in self["study_packages"]:
            for source in package["data_sources"]:
                indexed[source["source_id"]] = source
        return indexed

    # -- 法规版本 ------------------------------------------------------------

    def rule_in_force(self, on: str) -> dict:
        """返回某日有效（含当日）的法规版本；规则换版不产生溯及力。"""
        candidates = [
            rule
            for rule in self["rule_versions"]
            if rule["effective_from"] <= on
            and (rule["effective_to"] is None or on <= rule["effective_to"])
        ]
        if not candidates:
            raise ValueError(f"{on} 没有有效的法规版本")
        if len(candidates) > 1:
            raise ValueError(f"{on} 存在重叠生效的法规版本")
        return candidates[0]

    def term_end_for(self, claim_basis: str, grant_on: str, rule: dict) -> str:
        provision = _TERM_PROVISION[claim_basis]
        years = rule["provisions"][provision]
        return _add_years(grant_on, years)

    # -- 效力区间 ------------------------------------------------------------

    def interval_chain(self, claim_id: str) -> list[dict]:
        """按时间顺序返回一条主张的效力区间链，缺环或分叉即报错。"""
        intervals = [i for i in self["effect_intervals"] if i["claim_id"] == claim_id]
        by_id = {i["interval_id"]: i for i in intervals}
        heads = [i for i in intervals if i["supersedes"] is None]
        if len(heads) != 1:
            raise ValueError(f"主张 {claim_id} 的效力区间链必须恰好有一个起点")
        ordered = [heads[0]]
        while True:
            nxt = [i for i in intervals if i["supersedes"] == ordered[-1]["interval_id"]]
            if not nxt:
                break
            if len(nxt) > 1:
                raise ValueError(f"区间 {ordered[-1]['interval_id']} 被多段接替")
            ordered.append(nxt[0])
        if len(ordered) != len(intervals) or any(
            i["interval_id"] not in by_id for i in ordered
        ):
            raise ValueError(f"主张 {claim_id} 存在脱离接替链的区间")
        return ordered

    def has_intervals(self, claim_id: str) -> bool:
        return any(i["claim_id"] == claim_id for i in self["effect_intervals"])

    def segment_on(self, claim_id: str, on: str) -> dict | None:
        """某日适用的区间段；日期落在缝隙里说明区间链不连续。

        从未授予、没有任何区间的主张返回 None。
        """
        if not self.has_intervals(claim_id):
            return None
        chain = self.interval_chain(claim_id)
        for segment in chain:
            end = segment["end"]
            if segment["start"] <= on and (end is None or on < end):
                return segment
        return None

    def is_protected(self, claim_id: str, on: str) -> bool:
        segment = self.segment_on(claim_id, on)
        if segment is None:
            return False
        if segment["surrendered"]:
            return False
        return on < segment["term_end"]

    def active_intervals(self, on: str) -> list[dict]:
        result = []
        for claim_id in self.claims():
            segment = self.segment_on(claim_id, on)
            if segment and not segment["surrendered"] and on < segment["term_end"]:
                result.append(segment)
        return result

    def blocking_claims(self, application: dict, on: str) -> list[dict]:
        """同一活性成分、同一适应症、当日仍在保护（未放弃且未届满）的区间。"""
        blockers = []
        for segment in self.active_intervals(on):
            if application["target_indication_id"] not in segment["indication_ids"]:
                continue
            claim = self.claims()[segment["claim_id"]]
            product = self.products()[claim["product_id"]]
            if product["ingredient_code"] != application["target_ingredient_code"]:
                continue
            blockers.append(segment)
        return blockers

    # -- 冲突主张 ------------------------------------------------------------

    def _status_on(self, claim: dict, on: str) -> dict | None:
        """回放状态历史，返回截至某日最后一条已生效状态（只追加，不改写）。"""
        effective = None
        for entry in claim["status_history"]:
            if entry["on"] <= on:
                effective = entry
            else:
                break
        return effective

    def open_conflicts(self, on: str) -> list[dict]:
        """截至某日未裁决的权利冲突：同一产品上，一方处于 conflicting，
        且另一持有人的对抗主张仍停留在 asserted（未授予、未驳回）。
        双方主张都返回，供审评人员复核相互冲突的请求。
        """
        claims = list(self.claims().values())
        statuses = {c["claim_id"]: self._status_on(c, on) for c in claims}
        result: dict[str, dict] = {}
        for claim in claims:
            status = statuses[claim["claim_id"]]
            if status is None or status["status"] != "conflicting":
                continue
            result[claim["claim_id"]] = claim
            for other in claims:
                if other["product_id"] != claim["product_id"]:
                    continue
                if other["holder_id"] == claim["holder_id"]:
                    continue
                other_status = statuses[other["claim_id"]]
                if other_status is not None and other_status["status"] == "asserted":
                    result[other["claim_id"]] = other
        return list(result.values())

    def conflict_touches(self, claim: dict, application: dict) -> bool:
        product = self.products()[claim["product_id"]]
        return product["ingredient_code"] == application["target_ingredient_code"]

    # -- 证据（授权引用 / 补充提交） -----------------------------------------

    def citation_pending(self, application: dict, on: str) -> list[dict]:
        pending = []
        for source_id in application["citation_source_ids"]:
            source = self.data_sources().get(source_id)
            if source is None:
                pending.append(
                    {"code": "citation_source_missing", "detail": f"引用来源 {source_id} 不在研究包内", "source_id": source_id}
                )
                continue
            auth = source.get("authorization")
            if auth is None:
                pending.append(
                    {"code": "citation_without_authorization", "detail": f"{source_id} 缺少授权引用依据", "source_id": source_id}
                )
                continue
            if not (auth["valid_from"] <= on <= auth["valid_until"]):
                pending.append(
                    {
                        "code": "citation_authorization_expired",
                        "detail": f"授权引用函{auth['reference']}有效期为{auth['valid_from']}至{auth['valid_until']}",
                        "source_id": source_id,
                    }
                )
            if auth["scope_ingredient"] != application["target_ingredient_code"]:
                pending.append(
                    {
                        "code": "citation_scope_mismatch",
                        "detail": f"授权范围成分{auth['scope_ingredient']}与目标成分{application['target_ingredient_code']}不一致",
                        "source_id": source_id,
                    }
                )
        if application["relies_on_originator_data"] and not application["citation_source_ids"]:
            blocked = self.blocking_claims(application, on)
            if blocked:
                pending.append(
                    {
                        "code": "missing_authorization_or_wait",
                        "detail": "依赖原始试验数据但未附有效授权引用；如不补正须等待保护期届满",
                        "source_id": None,
                    }
                )
        return pending

    # -- 核定 ----------------------------------------------------------------

    def evaluate(self, application_id: str, decided_on: str) -> dict:
        """按决定当日的有效规则复核申请，返回（但不落盘）核定结论。

        结论优先级：法定例外受理 > 冲突暂缓 > 证据待补暂缓 > 等待届满 > 受理。
        """
        application = self.applications()[application_id]
        rule = self.rule_in_force(decided_on)
        reasons: list[str] = []
        pending: list[dict] = []
        basis_interval_ids: list[str] = []
        basis_claim_ids: list[str] = []
        basis_source_ids: list[str] = []
        basis_exception_ids: list[str] = []
        basis_decision_ids: list[str] = []
        basis_transfer_ids: list[str] = []

        blockers = self.blocking_claims(application, decided_on)
        for segment in blockers:
            basis_interval_ids.append(segment["interval_id"])
            basis_claim_ids.append(segment["claim_id"])
            # 沿效力区间链追溯收窄、转让等历次依据，保证复核能回到完整来龙去脉
            for ancestor in self.interval_chain(segment["claim_id"]):
                if ancestor["start"] > decided_on:
                    break
                if ancestor["basis"]["decision_id"]:
                    basis_decision_ids.append(ancestor["basis"]["decision_id"])
                if ancestor["basis"]["transfer_id"]:
                    basis_transfer_ids.append(ancestor["basis"]["transfer_id"])

        # 1) 法定例外（如公共健康强制许可）
        if application["invokes_exception_id"]:
            exception = self.exceptions()[application["invokes_exception_id"]]
            if exception["rule_id"] == rule["rule_id"]:
                basis_exception_ids.append(exception["exception_id"])
                reasons.append(f"主张{exception['title']}，符合{rule['title']}法定例外，保护期内可受理")
                return self._verdict(
                    application, decided_on, rule, "accept", None, pending, reasons,
                    basis_interval_ids, basis_claim_ids, basis_source_ids,
                    basis_exception_ids, basis_decision_ids, basis_transfer_ids,
                )

        # 2) 未裁决的冲突主张
        conflicts = [
            claim
            for claim in self.open_conflicts(decided_on)
            if self.conflict_touches(claim, application)
        ]
        if conflicts:
            for claim in conflicts:
                basis_claim_ids.append(claim["claim_id"])
            pending.append(
                {"code": "claim_conflict", "detail": "存在未裁决的冲突权利主张，需复核转让与研究包依据", "source_id": None}
            )
            reasons.append("冲突主张未裁决前暂缓受理")
            return self._verdict(
                application, decided_on, rule, "suspend", None, pending, reasons,
                basis_interval_ids, basis_claim_ids, basis_source_ids,
                basis_exception_ids, basis_decision_ids, basis_transfer_ids,
            )

        # 3) 授权引用 / 补充资料待补
        pending = self.citation_pending(application, decided_on)
        basis_source_ids.extend(application["citation_source_ids"])
        if pending and any(item["code"] != "missing_authorization_or_wait" for item in pending):
            reasons.append("授权引用等证据待补正，暂缓受理")
            return self._verdict(
                application, decided_on, rule, "suspend", None, pending, reasons,
                basis_interval_ids, basis_claim_ids, basis_source_ids,
                basis_exception_ids, basis_decision_ids, basis_transfer_ids,
            )

        # 4) 保护期内 → 等待届满
        if blockers:
            segment = blockers[0]
            claim = self.claims()[segment["claim_id"]]
            pending = []  # “等待届满”不是待补事项
            holder = self.holders()[segment["holder_id"]]["label"]
            reasons.append(
                f"{self.indications()[application['target_indication_id']]['label']}"
                f"处于{claim['claim_id']}保护期内，依据{rule['rule_id']}届满日{segment['term_end']}"
            )
            if segment["holder_id"] != claim["holder_id"]:
                reasons.append(f"当前权利人为{holder}（效力区间可溯至持有人变更记录）")
            return self._verdict(
                application, decided_on, rule, "wait", segment["term_end"], pending, reasons,
                basis_interval_ids, basis_claim_ids, basis_source_ids,
                basis_exception_ids, basis_decision_ids, basis_transfer_ids,
            )

        # 5) 无在效阻断 → 受理（含已撤回/已届满的历史区间，整链留存为依据）
        historical_claims = {segment["claim_id"] for segment in blockers}
        for claim_id, claim in self.claims().items():
            if not self.has_intervals(claim_id):
                continue
            chain = self.interval_chain(claim_id)
            latest = chain[-1]
            product = self.products()[claim["product_id"]]
            if product["ingredient_code"] != application["target_ingredient_code"]:
                continue
            if application["target_indication_id"] not in latest["indication_ids"]:
                continue
            if latest["surrendered"] and decided_on < latest["term_end"]:
                for segment in chain:
                    basis_interval_ids.append(segment["interval_id"])
                historical_claims.add(claim_id)
                surrender_submission = latest["basis"]["submission_id"]
                reasons.append(
                    f"{claim_id}保护已因撤回（{surrender_submission}）形成放弃区间"
                    f"{latest['interval_id']}，不再阻断；原授予区间整链留存可溯"
                )
            elif not latest["surrendered"] and latest["term_end"] <= decided_on:
                for segment in chain:
                    basis_interval_ids.append(segment["interval_id"])
                historical_claims.add(claim_id)
                reasons.append(
                    f"{claim_id}保护期已于{latest['term_end']}届满，全部区间成为历史记录"
                )
        for claim_id in historical_claims:
            basis_claim_ids.append(claim_id)
        if historical_claims or application["relies_on_originator_data"]:
            if not reasons:
                reasons.append("无在效保护区间阻断（历史授予区间仍留存可溯），可予受理")
        else:
            reasons.append("申请不依赖在保护数据，可予受理")
        return self._verdict(
            application, decided_on, rule, "accept", None, [], reasons,
            basis_interval_ids, basis_claim_ids, basis_source_ids,
            basis_exception_ids, basis_decision_ids, basis_transfer_ids,
        )

    def _verdict(
        self, application, decided_on, rule, outcome, wait_until, pending, reasons,
        interval_ids, claim_ids, source_ids, exception_ids, decision_ids, transfer_ids,
    ) -> dict:
        return {
            "application_id": application["application_id"],
            "decided_on": decided_on,
            "outcome": outcome,
            "wait_until": wait_until,
            "pending_items": pending,
            "reasons": reasons,
            "basis": {
                "rule_id": rule["rule_id"],
                "interval_ids": sorted(set(interval_ids)),
                "claim_ids": sorted(set(claim_ids)),
                "source_ids": sorted(set(source_ids)),
                "exception_ids": sorted(set(exception_ids)),
                "decision_ids": sorted(set(decision_ids)),
                "transfer_ids": sorted(set(transfer_ids)),
            },
        }

    def append_determination(
        self, application_id: str, decided_on: str,
        reviewer_id: str, approver_id: str,
        determination_id: str | None = None,
    ) -> dict:
        """追加一条核定记录。唯一的写入口：只增不改，历史核定永不被覆盖。"""
        verdict = self.evaluate(application_id, decided_on)
        existing_ids = {d["determination_id"] for d in self["determinations"]}
        if determination_id is None:
            determination_id = f"D-{len(self['determinations']) + 1}"
        if determination_id in existing_ids:
            raise ValueError("核定记录标识重复；不得覆盖既有记录")
        reviewer = self.staff().get(reviewer_id)
        approver = self.staff().get(approver_id)
        if reviewer is None or approver is None:
            raise ValueError("签批人必须是在册审评/批准人员")
        if approver["role"] != "approver":
            raise ValueError("核定记录的批准人角色必须为 approver")
        record = {
            "determination_id": determination_id,
            **verdict,
            "reviewer_id": reviewer_id,
            "approver_id": approver_id,
        }
        self["determinations"].append(record)
        return record

    def history_for(self, application_id: str) -> list[dict]:
        """同一申请的全部历次核定，按时间排列，旧结论原样保留。"""
        return [
            d
            for d in self["determinations"]
            if d["application_id"] == application_id
        ]

    # -- 视图 ----------------------------------------------------------------

    def applicant_view(self, holder_id: str) -> dict:
        """申请人可见：自身申请的结论与待补事项，以及可披露的保护范围。

        不披露原始研究数据与来源明细，只披露产品/适应症/权利人/届满日/依据法规。
        """
        scope = []
        for segment in self["effect_intervals"]:
            claim = self.claims()[segment["claim_id"]]
            product = self.products()[claim["product_id"]]
            scope.append(
                {
                    "claim_id": claim["claim_id"],
                    "ingredient": self.ingredients()[product["ingredient_code"]]["label"],
                    "dosage_form": product["dosage_form"],
                    "indications": [
                        self.indications()[i]["label"] for i in segment["indication_ids"]
                    ],
                    "holder": self.holders()[segment["holder_id"]]["label"],
                    "term_end": segment["term_end"],
                    "surrendered": segment["surrendered"],
                    "rule_id": segment["basis"]["rule_id"],
                    "effective_from": segment["start"],
                    "effective_to": segment["end"],
                }
            )
        my_applications = [
            a for a in self.applications().values()
            if a["applicant_holder_id"] == holder_id
        ]
        files = []
        for application in my_applications:
            for decision in self.history_for(application["application_id"]):
                files.append(
                    {
                        "application_id": application["application_id"],
                        "target_indication": self.indications()[application["target_indication_id"]]["label"],
                        "decided_on": decision["decided_on"],
                        "outcome": decision["outcome"],
                        "wait_until": decision["wait_until"],
                        "pending_items": decision["pending_items"],
                        "reasons": decision["reasons"],
                    }
                )
        return {"holder": self.holders()[holder_id]["label"], "protection_scope": scope, "my_files": files}

    def reviewer_view(self, on: str) -> dict:
        """审评人员视图：在效区间、未决冲突、全部主张链与核定依据。"""
        chains = {}
        for claim_id in self.claims():
            if not self.has_intervals(claim_id):
                chains[claim_id] = []  # 从未授予（如被驳回的冲突主张）没有区间链
                continue
            chains[claim_id] = [
                {
                    "interval_id": segment["interval_id"],
                    "kind": segment["kind"],
                    "start": segment["start"],
                    "end": segment["end"],
                    "term_end": segment["term_end"],
                    "holder_id": segment["holder_id"],
                    "indication_ids": segment["indication_ids"],
                    "surrendered": segment["surrendered"],
                    "basis": segment["basis"],
                }
                for segment in self.interval_chain(claim_id)
            ]
        return {
            "as_of": on,
            "rule_in_force": self.rule_in_force(on)["rule_id"],
            "active_intervals": [i["interval_id"] for i in self.active_intervals(on)],
            "open_conflicts": [c["claim_id"] for c in self.open_conflicts(on)],
            "chains": chains,
            "determinations": [d["determination_id"] for d in self["determinations"]],
        }


# ---------------------------------------------------------------------------
# 完整性校验
# ---------------------------------------------------------------------------

def _require(value: dict, field: str, errors: list[str]) -> None:
    if field not in value:
        errors.append(f"缺少必要字段: {field}")


def _check_refs(book: dict, errors: list[str]) -> None:
    def ids(key: str, id_field: str) -> set:
        return {item[id_field] for item in book.get(key, [])}

    rules = ids("rule_versions", "rule_id")
    staff = ids("staff", "staff_id")
    holders = ids("holders", "holder_id")
    ingredients = ids("active_ingredients", "ingredient_code")
    indication_ids = ids("indications", "indication_id")
    product_ids = ids("products", "product_id")
    package_ids = ids("study_packages", "package_id")
    submission_ids = ids("submissions", "submission_id")
    claim_ids = ids("claims", "claim_id")
    exception_ids = ids("legal_exceptions", "exception_id")
    transfer_ids = ids("transfers", "transfer_id")
    decision_ids = ids("court_decisions", "decision_id")
    application_ids = ids("applications", "application_id")
    source_ids = {
        s["source_id"]
        for package in book.get("study_packages", [])
        for s in package["data_sources"]
    }

    for product in book.get("products", []):
        if product["ingredient_code"] not in ingredients:
            errors.append(f"产品 {product['product_id']} 的活性成分不存在")
        if product.get("parent_product_id") and product["parent_product_id"] not in product_ids:
            errors.append(f"产品 {product['product_id']} 的父产品不存在")
        if product["holder_id"] not in holders:
            errors.append(f"产品 {product['product_id']} 的持有人不存在")
    for indication in book.get("indications", []):
        if indication["ingredient_code"] not in ingredients:
            errors.append(f"适应症 {indication['indication_id']} 的活性成分不存在")
    for package in book.get("study_packages", []):
        if package["product_id"] not in product_ids:
            errors.append(f"研究包 {package['package_id']} 的产品不存在")
    for submission in book.get("submissions", []):
        if submission["product_id"] not in product_ids:
            errors.append(f"提交 {submission['submission_id']} 的产品不存在")
        if submission.get("package_id") and submission["package_id"] not in package_ids:
            errors.append(f"提交 {submission['submission_id']} 的研究包不存在")
    for claim in book.get("claims", []):
        if claim["product_id"] not in product_ids:
            errors.append(f"主张 {claim['claim_id']} 的产品不存在")
        if claim["holder_id"] not in holders:
            errors.append(f"主张 {claim['claim_id']} 的持有人不存在")
        if claim.get("submission_id") and claim["submission_id"] not in submission_ids:
            errors.append(f"主张 {claim['claim_id']} 关联的提交不存在")
        dates = [h["on"] for h in claim["status_history"]]
        if dates != sorted(dates):
            errors.append(f"主张 {claim['claim_id']} 状态历史必须按日期只追加排列")
    for exception in book.get("legal_exceptions", []):
        if exception["rule_id"] not in rules:
            errors.append(f"法定例外 {exception['exception_id']} 的法规版本不存在")
    for transfer in book.get("transfers", []):
        if transfer["from_holder_id"] not in holders or transfer["to_holder_id"] not in holders:
            errors.append(f"转让 {transfer['transfer_id']} 的持有人不存在")
        if any(pid not in product_ids for pid in transfer["product_ids"]):
            errors.append(f"转让 {transfer['transfer_id']} 含不存在的产品")
        if transfer["record_submission_id"] not in submission_ids:
            errors.append(f"转让 {transfer['transfer_id']} 缺少备案提交")
    for decision in book.get("court_decisions", []):
        if decision["claim_id"] not in claim_ids:
            errors.append(f"法院决定 {decision['decision_id']} 的主张不存在")
        if any(i not in indication_ids for i in decision["effect"]["remove_indication_ids"]):
            errors.append(f"法院决定 {decision['decision_id']} 移除了不存在的适应症")
    for interval in book.get("effect_intervals", []):
        if interval["claim_id"] not in claim_ids:
            errors.append(f"区间 {interval['interval_id']} 的主张不存在")
        if interval["holder_id"] not in holders:
            errors.append(f"区间 {interval['interval_id']} 的持有人不存在")
        if any(pid not in product_ids for pid in interval["product_ids"]):
            errors.append(f"区间 {interval['interval_id']} 含不存在的产品")
        if any(i not in indication_ids for i in interval["indication_ids"]):
            errors.append(f"区间 {interval['interval_id']} 含不存在的适应症")
        basis = interval["basis"]
        if basis["rule_id"] not in rules:
            errors.append(f"区间 {interval['interval_id']} 依据的法规不存在")
        if basis.get("submission_id") and basis["submission_id"] not in submission_ids:
            errors.append(f"区间 {interval['interval_id']} 依据的提交不存在")
        if basis.get("transfer_id") and basis["transfer_id"] not in transfer_ids:
            errors.append(f"区间 {interval['interval_id']} 依据的转让不存在")
        if basis.get("decision_id") and basis["decision_id"] not in decision_ids:
            errors.append(f"区间 {interval['interval_id']} 依据的法院决定不存在")
        if interval["supersedes"] and not any(
            i["interval_id"] == interval["supersedes"] for i in book["effect_intervals"]
        ):
            errors.append(f"区间 {interval['interval_id']} 的接替目标不存在")
    for application in book.get("applications", []):
        if application["applicant_holder_id"] not in holders:
            errors.append(f"申请 {application['application_id']} 的申请人不存在")
        if application["target_ingredient_code"] not in ingredients:
            errors.append(f"申请 {application['application_id']} 的目标成分不存在")
        if application["target_indication_id"] not in indication_ids:
            errors.append(f"申请 {application['application_id']} 的目标适应症不存在")
        if application.get("target_product_id") and application["target_product_id"] not in product_ids:
            errors.append(f"申请 {application['application_id']} 的目标产品不存在")
        if any(sid not in source_ids for sid in application["citation_source_ids"]):
            errors.append(f"申请 {application['application_id']} 引用了不存在的数据源")
        if application.get("invokes_exception_id") and application["invokes_exception_id"] not in exception_ids:
            errors.append(f"申请 {application['application_id']} 主张的法定例外不存在")
    for decision in book.get("determinations", []):
        if decision["application_id"] not in application_ids:
            errors.append(f"核定 {decision['determination_id']} 的申请不存在")
        if decision["reviewer_id"] not in staff or decision["approver_id"] not in staff:
            errors.append(f"核定 {decision['determination_id']} 的签批人不存在")
        basis = decision["basis"]
        if basis["rule_id"] not in rules:
            errors.append(f"核定 {decision['determination_id']} 依据的法规不存在")
        if any(i not in {x['interval_id'] for x in book['effect_intervals']} for i in basis["interval_ids"]):
            errors.append(f"核定 {decision['determination_id']} 引用了不存在的区间")
        if any(c not in claim_ids for c in basis["claim_ids"]):
            errors.append(f"核定 {decision['determination_id']} 引用了不存在的主张")
        if decision["outcome"] == "wait" and not decision["wait_until"]:
            errors.append(f"核定 {decision['determination_id']} 为等待结论但缺少届满日")


def _check_rules_and_chains(book: dict, errors: list[str]) -> None:
    rules = sorted(book["rule_versions"], key=lambda r: r["effective_from"])
    for earlier, later in zip(rules, rules[1:]):
        if earlier["effective_to"] is None or earlier["effective_to"] >= later["effective_from"]:
            errors.append(f"法规版本 {earlier['rule_id']} 与 {later['rule_id']} 生效区间重叠")
        if later.get("retroactive"):
            errors.append(f"法规版本 {later['rule_id']} 标记为溯及既往，违反不溯及原则")

    products = {p["product_id"]: p for p in book["products"]}
    claims = {c["claim_id"]: c for c in book["claims"]}
    for claim_id, claim in claims.items():
        history_statuses = {h["status"] for h in claim["status_history"]}
        chain = sorted(
            (i for i in book["effect_intervals"] if i["claim_id"] == claim_id),
            key=lambda i: i["start"],
        )
        granted_statuses = {"granted", "narrowed", "withdrawn"}
        ever_granted = bool(history_statuses & granted_statuses)
        if not chain:
            if ever_granted:
                errors.append(f"主张 {claim_id} 曾被授予但没有任何效力区间")
            continue
        if not ever_granted:
            errors.append(f"主张 {claim_id} 从未授予却存在效力区间")
            continue
        term_end = chain[0]["term_end"]
        grant = chain[0]
        rule = next(r for r in rules if r["rule_id"] == grant["basis"]["rule_id"])
        expected_term = _add_years(
            grant["start"],
            rule["provisions"][_TERM_PROVISION[claims[claim_id]["basis"]]],
        )
        if expected_term != term_end:
            errors.append(
                f"主张 {claim_id} 届满日 {term_end} 与授予时规则 {rule['rule_id']} 应得 {expected_term} 不符"
            )
        if not (rule["effective_from"] <= grant["start"] and
                (rule["effective_to"] is None or grant["start"] <= rule["effective_to"])):
            errors.append(f"主张 {claim_id} 的授予区间适用了当日尚未生效的法规")
        previous = None
        for segment in chain:
            if segment["term_end"] != term_end:
                errors.append(f"主张 {claim_id} 的区间 {segment['interval_id']} 改动了法定届满日")
            if previous is not None:
                if segment["supersedes"] != previous["interval_id"]:
                    errors.append(f"区间 {segment['interval_id']} 未正确接替 {previous['interval_id']}")
                if segment["start"] != previous["end"]:
                    errors.append(f"主张 {claim_id} 的区间在 {previous['end']}→{segment['start']} 出现缝隙或重叠")
            claim = claims[claim_id]
            for pid in segment["product_ids"]:
                if products[pid]["ingredient_code"] != products[claim["product_id"]]["ingredient_code"]:
                    errors.append(f"区间 {segment['interval_id']} 跨越了不同活性成分")
            previous = segment
        surrender_segments = [s for s in chain if s["surrendered"]]
        if len(surrender_segments) > 1:
            errors.append(f"主张 {claim_id} 存在多个放弃区间")
        if surrender_segments and surrender_segments[0] is not chain[-1]:
            errors.append(f"主张 {claim_id} 放弃后又出现有效区间")

    # 核定记录：作出当日规则必须在效；wait 的届满日必须与阻断区间一致
    intervals = {i["interval_id"]: i for i in book["effect_intervals"]}
    for decision in book["determinations"]:
        on = decision["decided_on"]
        in_force = next(
            (r for r in rules if r["effective_from"] <= on and
             (r["effective_to"] is None or on <= r["effective_to"])),
            None,
        )
        if in_force is None:
            errors.append(f"核定 {decision['determination_id']} 作出日没有有效法规")
        elif decision["basis"]["rule_id"] != in_force["rule_id"]:
            errors.append(
                f"核定 {decision['determination_id']} 依据 {decision['basis']['rule_id']}，"
                f"但当日有效法规为 {in_force['rule_id']}"
            )
        if decision["outcome"] == "wait":
            terms = {intervals[i]["term_end"] for i in decision["basis"]["interval_ids"]}
            if terms and decision["wait_until"] not in terms:
                errors.append(
                    f"核定 {decision['determination_id']} 的等待届满日与阻断区间 term_end 不一致"
                )
    # 核定只追加：同一申请的多条记录日期必须不降序
    histories: dict[str, list[str]] = {}
    for decision in book["determinations"]:
        histories.setdefault(decision["application_id"], []).append(decision["decided_on"])
    for application_id, dates in histories.items():
        if dates != sorted(dates):
            errors.append(f"申请 {application_id} 的核定记录未按时间只追加排列")
    ids_used = [d["determination_id"] for d in book["determinations"]]
    if len(ids_used) != len(set(ids_used)):
        errors.append("核定记录标识重复")


def validate_book(book: dict) -> None:
    errors: list[str] = []
    for field in _REQUIRED:
        _require(book, field, errors)
    if errors:
        raise ValueError("共享资料缺少必要字段: " + ", ".join(sorted(errors)))
    if book["version"] < 1 or len(book["actors"]) < 2 or len(book["facts"]) < 2 or len(book["constraints"]) < 2:
        raise ValueError("共享资料内容不完整")
    _check_refs(book, errors)
    _check_rules_and_chains(book, errors)
    if errors:
        raise ValueError("共享资料校验失败:\n- " + "\n- ".join(errors))


def load_domain(path: Path) -> RecordBook:
    """读取并校验业务资料，返回可用于核定与复核的资料册。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_book(value)
    return RecordBook(value)
