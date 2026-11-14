"""调用限流降级业务规则：状态流转、字段校验与筛选口径都收在这里。

限流降级的裁决与编排共用同一规则版本：拿到编排结果时调用
``orchestration_rules.limiter_degrade_decision``，规则版本不一致就拒绝降级。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.orchestration_rules import RULE_VERSION, limiter_degrade_decision
from app.store import store

MODULE = "limiter"
REQUIRED_FIELDS = ["规则编号", "限流对象", "限流算法"]
STATUS_ORDER = ["草稿", "生效中", "已触发", "已关闭"]
ACTION_RULES = {"启用限流": "生效中", "触发降级": "已触发", "关闭限流": "已关闭"}
NEGATIVE_ACTIONS = ["触发降级"]


class LimiterService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self.present(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["rule_version"] = RULE_VERSION
        entry["degrade_decision"] = None
        rows.append(entry)
        return self.present(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        orchestration_report: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"限流规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于调用限流降级可执行范围"

        decision: dict[str, Any] | None = None
        if action == "触发降级":
            if orchestration_report is not None:
                decision = limiter_degrade_decision(orchestration_report)
                if decision["decision"] != "降级":
                    entry["degrade_decision"] = decision
                    return None, f"降级裁决未通过：{decision['reason']}（规则版本 {RULE_VERSION}）"
            else:
                # 独立限流规则没有编排结果时，仍沿用同一规则版本裁决，不另起一套规则。
                decision = {
                    "rule_version": RULE_VERSION,
                    "rule_id": "ORCH-LIMITER-001",
                    "decision": "降级",
                    "reason": "未提供编排结果，按当前规则版本直接触发降级",
                }
            entry["degrade_decision"] = decision

        entry["status"] = ACTION_RULES[action]
        entry["pending"] = entry["status"] != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["rule_version"] = RULE_VERSION
        return self.present(entry), f"限流规则已{action}"

    def present(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = deepcopy(entry)
        row["rule_version"] = entry.get("rule_version") or RULE_VERSION
        row["降级裁决"] = entry.get("degrade_decision")
        return row
