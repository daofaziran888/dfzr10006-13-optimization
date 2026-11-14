"""服务编排组合业务规则：状态流转、预演判定、执行与回滚都收在这里。

预演、真实执行和失败回滚共用 ``orchestration_rules`` 的同一规则表与同一份
判定实现；执行时固化 ``plan_snapshot``，回滚只读该快照，不再读取当前编排步骤。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.orchestration_rules import (
    DEFAULT_TIMEOUT_SECONDS,
    OUTCOME_ARBITRATED,
    EXECUTED_OUTCOME,
    RULE_VERSION,
    STATUS_ROLLED_BACK,
    build_execution_log,
    compile_plan,
    evaluate_action,
    log_is_consistent,
    normalize_execution_log,
    plan_digest,
    rules_document,
)
from app.store import store

MODULE = "orchestra"
REQUIRED_FIELDS = ["流程编号", "流程名称", "编排步骤"]
STATUS_ORDER = ["草稿", "运行中", "已完成", "已回滚"]
ACTION_RULES = {"启动编排": "运行中", "回滚流程": "已回滚", "归档流程": "已完成"}
NEGATIVE_ACTIONS = ["回滚流程"]
TERMINAL_STATUSES = {"已完成", "已回滚"}


def _parse_timeout(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


class OrchestraService:
    # ---------- 查询 ----------

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
            rows = [
                row
                for row in rows
                if keyword in str(row.get("流程编号", "")) or keyword in str(row.get("流程名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self.present(row) if row is not None else None

    def rules(self, stage: str | None = None) -> dict[str, Any]:
        return rules_document(stage)

    # ---------- 登记与修改 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if self._is_blank(values.get(field))]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        }
        for field in ("流程编号", "流程名称", "编排步骤", "并行策略", "超时控制", "失败回滚"):
            if field in values:
                entry[field] = values[field]
        entry.setdefault("并行策略", {"mode": "并行", "groups": []})
        entry.setdefault("超时控制", DEFAULT_TIMEOUT_SECONDS)
        entry.setdefault("失败回滚", "失败或超时后按执行快照回滚")
        entry.update(
            {
                "status": STATUS_ORDER[0],
                "流程状态": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "执行日志": [],
                "rule_version": RULE_VERSION,
                "plan_snapshot": None,
                "plan_digest": "",
                "last_execution": None,
                "rollback_history": [],
            }
        )
        rows.append(entry)
        return self.present(entry), []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"编排流程 {entry_id} 不存在或已归档"
        if entry.get("status") in TERMINAL_STATUSES:
            return None, f"编排流程 {entry_id} 已是{entry.get('status')}状态，不能再修改步骤"
        changed = False
        for field in ("流程编号", "流程名称", "编排步骤", "并行策略", "超时控制", "失败回滚"):
            if field in values:
                entry[field] = values[field]
                changed = True
        if not changed:
            return None, "没有提交可修改的编排字段"
        entry["rule_version"] = RULE_VERSION
        return self.present(entry), "编排流程已更新"

    # ---------- 动作 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """保留原动作入口：返回 (流程, 说明)，供旧调用方使用。"""
        entry, message, _ = self.perform(entry_id, action)
        return entry, message

    def perform(
        self,
        entry_id: int,
        action: str,
        *,
        timeout_seconds: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """预演、执行、回滚、启动、归档的统一动作入口。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"编排流程 {entry_id} 不存在或已归档", {
                "ok": False,
                "allowed": False,
                "outcome": "已拒绝",
                "message": f"编排流程 {entry_id} 不存在或已归档",
                "rule_version": RULE_VERSION,
            }

        limit = timeout_seconds if timeout_seconds is not None else _parse_timeout(entry.get("超时控制"))
        report = evaluate_action(entry, action, limit)

        if not report.get("allowed"):
            return None, report["message"], report
        if not report.get("ok"):
            return None, report["message"], report

        if action == "启动编排":
            self._apply_start(entry, report)
        elif action == "执行编排":
            self._apply_execution(entry, report)
        elif action == "回滚流程":
            self._apply_rollback(entry, report)
        elif action == "归档流程":
            self._apply_archive(entry, report)
        # 预演编排只读，不改流程状态。

        entry["rule_version"] = RULE_VERSION
        return self.present(entry), report["message"], report

    # ---------- 应用动作 ----------

    def _apply_start(self, entry: dict[str, Any], report: dict[str, Any]) -> None:
        entry["status"] = "运行中"
        entry["流程状态"] = "运行中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["plan_snapshot"] = None
        entry["plan_digest"] = ""
        entry["last_execution"] = None
        entry["rollback_history"] = []
        self._append_log(
            entry,
            {
                "stage": "启动",
                "outcome": "已受理",
                "completed": False,
                "failed": False,
                "executed_steps": [],
                "timed_out_steps": [],
                "blocked_steps": [],
                "rule_version": RULE_VERSION,
                "message": "编排流程已启动，等待预演或真实执行",
            },
        )
        report.update({"executed": True, "status_after": "运行中", "message": "编排流程已启动，等待预演或真实执行"})

    def _apply_execution(self, entry: dict[str, Any], report: dict[str, Any]) -> None:
        snapshot = report.get("plan_snapshot")
        if isinstance(snapshot, dict):
            entry["plan_snapshot"] = deepcopy(snapshot)
            entry["plan_digest"] = str(report.get("plan_digest") or plan_digest(snapshot))

        succeeded = report.get("outcome") in {EXECUTED_OUTCOME, OUTCOME_ARBITRATED}
        executed_ids = [step["id"] for step in report.get("executable_steps", [])]
        event = build_execution_log(report)
        event["executed_steps"] = executed_ids
        self._append_log(entry, event)

        entry["last_execution"] = {
            "executed_steps": executed_ids,
            "timed_out_steps": [step["id"] for step in report.get("timed_out_steps", [])],
            "blocked_steps": [step["id"] for step in report.get("blocked_steps", [])],
            "outcome": report.get("outcome"),
            "plan_digest": entry.get("plan_digest", ""),
            "rule_version": RULE_VERSION,
        }
        if succeeded:
            entry["status"] = "已完成"
            entry["流程状态"] = "已完成"
            entry["pending"] = False
            entry["abnormal"] = False
            report["message"] = (
                f"真实执行完成：{len(executed_ids)} 步可执行，执行日志已按单一结果记录"
            )
        else:
            entry["status"] = "运行中"
            entry["流程状态"] = "运行中"
            entry["pending"] = True
            entry["abnormal"] = True
            report["message"] = (
                f"真实执行失败：{len(executed_ids)} 步可执行，"
                f"{len(report.get('blocked_steps', []))} 步阻塞，"
                f"{len(report.get('timed_out_steps', []))} 步超时，需回滚"
            )
        report.update(
            {
                "executed": succeeded,
                "status_after": entry["status"],
                "execution_log_consistent": log_is_consistent(
                    normalize_execution_log(entry.get("执行日志"))
                ),
            }
        )

    def _apply_rollback(self, entry: dict[str, Any], report: dict[str, Any]) -> None:
        entry["status"] = STATUS_ROLLED_BACK
        entry["流程状态"] = STATUS_ROLLED_BACK
        entry["pending"] = False
        entry["abnormal"] = True
        history = entry.get("rollback_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "rollback_order": list(report.get("rollback_order") or []),
                "plan_digest": report.get("plan_digest", ""),
                "rule_version": RULE_VERSION,
                "snapshot_differs_from_current": bool(report.get("snapshot_differs_from_current")),
            }
        )
        entry["rollback_history"] = history
        self._append_log(
            entry,
            {
                "stage": "回滚",
                "outcome": "已回滚",
                "completed": False,
                "failed": False,
                "executed_steps": list(report.get("rollback_order") or []),
                "timed_out_steps": [],
                "blocked_steps": [],
                "plan_digest": report.get("plan_digest", ""),
                "rule_version": RULE_VERSION,
                "message": report.get("message", ""),
            },
        )
        report.update({"executed": True, "status_after": "已回滚"})

    def _apply_archive(self, entry: dict[str, Any], report: dict[str, Any]) -> None:
        entry["status"] = "已完成"
        entry["流程状态"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = False
        self._append_log(
            entry,
            {
                "stage": "归档",
                "outcome": "已归档",
                "completed": True,
                "failed": False,
                "executed_steps": [],
                "timed_out_steps": [],
                "blocked_steps": [],
                "rule_version": RULE_VERSION,
                "message": "编排流程已归档",
            },
        )
        report.update({"executed": True, "status_after": "已完成", "message": "编排流程已归档"})

    # ---------- 展示 ----------

    @staticmethod
    def _is_blank(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, dict, tuple, set)):
            return len(value) == 0
        return False

    def _append_log(self, entry: dict[str, Any], event: dict[str, Any]) -> None:
        events = normalize_execution_log(entry.get("执行日志"))
        events.append(event)
        entry["执行日志"] = events

    def present(self, entry: dict[str, Any]) -> dict[str, Any]:
        """把内部结构整理成列表可直接展示的字段，并补齐规则版本。"""
        row = deepcopy(entry)
        snapshot, error = compile_plan(entry)
        events = normalize_execution_log(entry.get("执行日志"))

        if snapshot is not None:
            groups = snapshot["parallel_policy"]["groups"]
            step_count = len(snapshot["steps"])
            group_text = "、".join(
                f"{group['id']}({len(group['steps'])} 步)" for group in groups
            )
            row["编排步骤"] = f"{step_count} 步，分 {len(groups)} 组"
            row["并行策略"] = f"{len(groups)} 组并行：{group_text}"
            row["并行分组"] = groups
            row["编排步骤详情"] = snapshot["steps"]
        else:
            row.setdefault("编排步骤", "")
            row.setdefault("并行策略", "")
            row["并行分组"] = []
            row["编排步骤详情"] = []
            row["编排步骤解析错误"] = error or "编排步骤未结构化"

        limit = _parse_timeout(entry.get("超时控制")) or DEFAULT_TIMEOUT_SECONDS
        row["超时控制"] = f"{limit} 秒"
        row["失败回滚"] = str(entry.get("失败回滚") or "按执行快照回滚")
        row["流程状态"] = entry.get("status")
        row["rule_version"] = entry.get("rule_version") or RULE_VERSION

        if events:
            latest = events[-1]
            row["执行日志"] = f"{len(events)} 条，最近一次：{latest.get('outcome', '未知')}"
        else:
            row["执行日志"] = "暂无执行日志"
        row["执行日志明细"] = events
        row["执行日志一致"] = log_is_consistent(events)

        snapshot_value = entry.get("plan_snapshot")
        if isinstance(snapshot_value, dict):
            row["执行快照摘要"] = {
                "plan_digest": entry.get("plan_digest") or plan_digest(snapshot_value),
                "steps": [step["id"] for step in snapshot_value.get("steps", [])],
                "source": "执行时固化",
            }
        else:
            row["执行快照摘要"] = None
        return row
