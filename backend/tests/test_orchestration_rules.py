"""服务编排规则核对：90 秒超时 + 两组并行步骤。

运行：cd backend && .venv/bin/python -m unittest tests.test_orchestration_rules -v
"""
from __future__ import annotations

import copy
import unittest

from app.seed import SEED_ROWS
from app.services import orchestration_rules as rules
from app.services.limiter import LimiterService
from app.services.orchestra import OrchestraService


def reset_module(module: str) -> None:
    """每个用例使用独立的内存表副本，避免用例之间互相污染。"""
    import app.store as store_module

    store_module.store._tables[module] = [copy.deepcopy(row) for row in SEED_ROWS[module]]


TWO_GROUP_STEPS = [
    {
        "id": "S1",
        "name": "拉取交易流水",
        "group": "G1",
        "estimated_seconds": 45,
        "resource": "对账库",
        "priority": 1,
    },
    {
        "id": "S2",
        "name": "拉取支付流水",
        "group": "G1",
        "estimated_seconds": 50,
        "resource": "对账库",
        "priority": 2,
    },
    {
        "id": "S3",
        "name": "差异比对",
        "group": "G2",
        "estimated_seconds": 120,
        "resource": "计算集群",
        "priority": 1,
    },
    {
        "id": "S4",
        "name": "生成对账报告",
        "group": "G2",
        "estimated_seconds": 30,
        "resource": "报表库",
        "priority": 1,
        "depends_on": ["S3"],
    },
]

TWO_GROUPS = [
    {"id": "G1", "name": "流水拉取", "mode": "并行", "steps": ["S1", "S2"]},
    {"id": "G2", "name": "比对汇总", "mode": "并行", "steps": ["S3", "S4"]},
]


def make_entry(**overrides) -> dict:
    entry = {
        "id": 1,
        "status": "运行中",
        "流程编号": "ORCH-TEST",
        "流程名称": "测试编排",
        "编排步骤": copy.deepcopy(TWO_GROUP_STEPS),
        "并行策略": {"mode": "并行", "groups": copy.deepcopy(TWO_GROUPS)},
        "超时控制": 90,
        "执行日志": [],
        "plan_snapshot": None,
        "last_execution": None,
    }
    entry.update(overrides)
    return entry


class SharedRuleTableTests(unittest.TestCase):
    def test_single_rule_version_shared_by_all_stages(self) -> None:
        document = rules.rules_document()
        self.assertEqual(document["rule_version"], rules.RULE_VERSION)
        stages = {stage for item in document["entries"] for stage in item["stages"]}
        self.assertEqual(
            stages,
            {"启动", "预演", "执行", "回滚", "归档", "限流降级"},
        )

    def test_timeout_and_parallel_conditions_are_in_the_shared_table(self) -> None:
        ids = {item["id"] for item in rules.rule_entries("预演")}
        self.assertIn("ORCH-TIMEOUT-001", ids)
        self.assertIn("ORCH-PARALLEL-001", ids)
        self.assertIn("ORCH-RESOURCE-001", ids)
        rollback_ids = {item["id"] for item in rules.rule_entries("回滚")}
        self.assertIn("ORCH-ROLLBACK-001", rollback_ids)
        self.assertIn("ORCH-ROLLBACK-002", rollback_ids)


class TwoGroupTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = rules.evaluate_action(make_entry(), "预演编排", 90)

    def test_parallel_groups_are_compiled_before_timeout(self) -> None:
        self.assertEqual([group["id"] for group in self.report["parallel_groups"]], ["G1", "G2"])
        self.assertIn("ORCH-PARALLEL-001", self.report["rule_hits"])
        self.assertGreaterEqual(2, self.report["group_count"])

    def test_timeout_uses_90_seconds(self) -> None:
        self.assertEqual(self.report["timeout_seconds"], 90)
        self.assertEqual([step["id"] for step in self.report["timed_out_steps"]], ["S3"])
        self.assertEqual(
            self.report["executable_steps"][0]["estimated_seconds"],
            45,
        )

    def test_priority_arbitration_before_timeout(self) -> None:
        self.assertEqual(
            [step["id"] for step in self.report["executable_steps"]],
            ["S1"],
        )
        blocked = {
            step["id"]: step["rule_id"] for step in self.report["blocked_steps"]
        }
        self.assertEqual(blocked["S2"], "ORCH-RESOURCE-001")
        self.assertEqual(blocked["S4"], "ORCH-DEPEND-001")
        priority_winner = next(
            step
            for step in self.report["checklist"]
            if step["id"] == "S1"
        )
        self.assertEqual(priority_winner["priority"], 1)

    def test_outcome_requires_rollback_and_lists_both_kinds(self) -> None:
        self.assertEqual(self.report["outcome"], rules.OUTCOME_NEEDS_ROLLBACK)
        self.assertTrue(self.report["must_rollback"])
        statuses = {step["id"]: step["status"] for step in self.report["checklist"]}
        self.assertEqual(
            statuses,
            {"S1": "可执行", "S2": "阻塞", "S3": "已超时", "S4": "阻塞"},
        )

    def test_timeout_threshold_is_inclusive(self) -> None:
        entry = make_entry()
        entry["编排步骤"][2]["estimated_seconds"] = 90
        report = rules.evaluate_action(entry, "预演编排", 90)
        self.assertEqual(report["timed_out_steps"], [])
        entry["编排步骤"][2]["estimated_seconds"] = 91
        report = rules.evaluate_action(entry, "预演编排", 90)
        self.assertEqual([step["id"] for step in report["timed_out_steps"]], ["S3"])


class GateAndRollbackTests(unittest.TestCase):
    def test_draft_cannot_dry_run(self) -> None:
        report = rules.evaluate_action(make_entry(status="草稿"), "预演编排", 90)
        self.assertFalse(report["allowed"])
        self.assertEqual(report["outcome"], rules.OUTCOME_REJECTED)
        self.assertIn("ORCH-GATE-001", report["rule_hits"])

    def test_completed_flow_cannot_roll_back_again(self) -> None:
        report = rules.evaluate_action(make_entry(status="已完成"), "回滚流程")
        self.assertFalse(report["allowed"])
        self.assertEqual(report["outcome"], rules.OUTCOME_REJECTED)

    def test_rolled_back_flow_cannot_roll_back_again(self) -> None:
        report = rules.evaluate_action(make_entry(status="已回滚"), "回滚流程")
        self.assertFalse(report["allowed"])

    def test_rollback_reads_frozen_snapshot_not_current_steps(self) -> None:
        entry = make_entry()
        execution = rules.evaluate_action(entry, "执行编排", 90)
        entry["plan_snapshot"] = execution["plan_snapshot"]
        entry["plan_digest"] = execution["plan_digest"]
        entry["last_execution"] = {
            "executed_steps": [step["id"] for step in execution["executable_steps"]],
        }
        # 执行后编排步骤被改写，回滚仍应按执行快照补偿 S1。
        entry["编排步骤"] = [
            {"id": "S9", "group": "G1", "estimated_seconds": 1},
            {"id": "S10", "group": "G2", "estimated_seconds": 1},
        ]
        report = rules.evaluate_action(entry, "回滚流程")
        self.assertTrue(report["allowed"])
        self.assertEqual(report["rollback_order"], ["S1"])
        self.assertEqual(
            [step["id"] for step in report["plan_snapshot"]["steps"]],
            ["S1", "S2", "S3", "S4"],
        )
        self.assertTrue(report["snapshot_differs_from_current"])

    def test_rollback_without_snapshot_is_rejected(self) -> None:
        report = rules.evaluate_action(make_entry(), "回滚流程")
        self.assertTrue(report["allowed"])
        self.assertFalse(report["ok"])
        self.assertIn("ORCH-ROLLBACK-002", report["rule_hits"])


class ExecutionLogTests(unittest.TestCase):
    def test_log_is_single_outcome_when_timeout(self) -> None:
        entry = make_entry()
        execution = rules.evaluate_action(entry, "执行编排", 90)
        log = rules.build_execution_log(execution)
        self.assertTrue(log["failed"])
        self.assertFalse(log["completed"])
        self.assertEqual(log["timed_out_steps"], ["S3"])
        self.assertEqual(log["blocked_steps"], ["S2", "S4"])
        self.assertEqual(log["rule_version"], rules.RULE_VERSION)

    def test_legacy_log_with_completed_and_failed_is_normalized(self) -> None:
        legacy = [
            {
                "stage": "执行",
                "outcome": "已完成",
                "completed": True,
                "failed": True,
                "completed_steps": ["S1"],
                "failed_steps": ["S3"],
            }
        ]
        events = rules.normalize_execution_log(legacy)
        self.assertEqual(events[0]["outcome"], "失败")
        self.assertFalse(events[0]["completed"])
        self.assertTrue(events[0]["failed"])
        self.assertTrue(rules.log_is_consistent(events))

    def test_service_keeps_log_consistent_after_execution(self) -> None:
        reset_module("orchestra")
        service = OrchestraService()
        entry, missing = service.create_entry(
            {
                "流程编号": "ORCH-TEST",
                "流程名称": "测试编排",
                "编排步骤": copy.deepcopy(TWO_GROUP_STEPS),
                "并行策略": {"mode": "并行", "groups": copy.deepcopy(TWO_GROUPS)},
                "超时控制": 90,
            }
        )
        self.assertEqual(missing, [])
        assert entry is not None
        service.perform(entry["id"], "启动编排")
        entry, message, report = service.perform(entry["id"], "执行编排")
        assert entry is not None
        self.assertEqual(report["outcome"], rules.OUTCOME_NEEDS_ROLLBACK)
        self.assertTrue(entry["执行日志一致"])
        latest = entry["执行日志明细"][-1]
        self.assertTrue(latest["failed"])
        self.assertFalse(latest["completed"])


class LimiterVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_module("limiter")

    def test_same_version_degrades(self) -> None:
        service = LimiterService()
        report = {
            "rule_version": rules.RULE_VERSION,
            "outcome": rules.OUTCOME_NEEDS_ROLLBACK,
            "flow_code": "ORCH-TEST",
            "executable_steps": [],
            "blocked_steps": [],
        }
        entry, message = service.run_action(1, "触发降级", orchestration_report=report)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["降级裁决"]["decision"], "降级")
        self.assertEqual(entry["rule_version"], rules.RULE_VERSION)

    def test_stale_version_refuses_degrade(self) -> None:
        service = LimiterService()
        report = {
            "rule_version": "ORCH-RULES-2026.10.0",
            "outcome": rules.OUTCOME_NEEDS_ROLLBACK,
        }
        entry, message = service.run_action(1, "触发降级", orchestration_report=report)
        self.assertIsNone(entry)
        self.assertIn("规则版本", message)


class SeedChecklistTests(unittest.TestCase):
    """用 90 秒超时和两组并行步骤核对编排结果清单（ORCH-0001~0003）。"""

    def setUp(self) -> None:
        reset_module("orchestra")

    def test_seed_checklist(self) -> None:
        service = OrchestraService()
        checklist = {
            row["流程编号"]: row for row in service.list_entries(page=1, size=100) [0]
        }
        self.assertEqual(set(checklist), {"ORCH-0001", "ORCH-0002", "ORCH-0003"})
        self.assertEqual(len(checklist["ORCH-0001"]["并行分组"]), 2)
        self.assertEqual(len(checklist["ORCH-0002"]["并行分组"]), 2)
        self.assertTrue(all(row["执行日志一致"] for row in checklist.values()))
        self.assertEqual(checklist["ORCH-0001"]["流程状态"], "草稿")
        self.assertEqual(checklist["ORCH-0003"]["流程状态"], "已完成")

        execution = rules.evaluate_action(
            make_entry(**{"流程编号": "ORCH-0002", "status": "运行中"}),
            "预演编排",
            90,
        )
        self.assertEqual(len(execution["parallel_groups"]), 2)
        self.assertEqual(execution["timeout_seconds"], 90)
        self.assertEqual(
            [step["id"] for step in execution["executable_steps"]],
            ["S1"],
        )
        self.assertEqual(
            [step["id"] for step in execution["blocked_steps"]],
            ["S2", "S4"],
        )
        self.assertEqual(
            [step["id"] for step in execution["timed_out_steps"]],
            ["S3"],
        )


if __name__ == "__main__":
    unittest.main()
