"""服务编排组合的共用规则表与唯一判定实现。

预演（dry run）、真实执行和失败回滚都走本模块的同一份规则；调用限流降级
（``app/services/limiter.py``）也引用这里的 ``RULE_VERSION``，保证同一条流程在
不同入口不会使用不同规则版本。

规则表覆盖五类条件：

* 状态门禁：草稿不能进入预演，已完成/已回滚不能再次回滚；
* 超时：步骤预估耗时超过超时上限（默认 90 秒）即判为超时；
* 并行：并行分组在预演阶段一次性编译，不能在步骤超时之后才补判；
* 资源争用：同一并行分组内两个分支争用同一资源时，优先级数值小者先执行；
* 回滚：回滚只读取执行时固化的 ``plan_snapshot``，不读取当前编排步骤。
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

RULE_VERSION = "ORCH-RULES-2026.11.1"
DEFAULT_TIMEOUT_SECONDS = 90

STATUS_DRAFT = "草稿"
STATUS_RUNNING = "运行中"
STATUS_COMPLETED = "已完成"
STATUS_ROLLED_BACK = "已回滚"

EXECUTED_OUTCOME = "可执行"
OUTCOME_NEEDS_ROLLBACK = "需回滚"
OUTCOME_PARTIAL_BLOCKED = "部分阻塞"
OUTCOME_ALL_BLOCKED = "全部阻塞"
OUTCOME_ARBITRATED = "优先级让行"
OUTCOME_REJECTED = "已拒绝"
OUTCOME_INVALID = "编排步骤无效"
OUTCOME_ACCEPTED = "已受理"

FAILURE_OUTCOMES = {
    OUTCOME_NEEDS_ROLLBACK,
    OUTCOME_PARTIAL_BLOCKED,
    OUTCOME_ALL_BLOCKED,
}

# 动作 -> 状态门禁。status 是动作执行前必须满足的流程状态；预演与执行是只读动作，
# 不改变流程状态；回滚只允许运行中的失败流程。
ACTION_STATE_RULES: dict[str, dict[str, Any]] = {
    "启动编排": {
        "allowed_statuses": (STATUS_DRAFT,),
        "next_status": STATUS_RUNNING,
        "reject_message": "启动编排只允许草稿流程",
        "stage": "启动",
    },
    "预演编排": {
        "allowed_statuses": (STATUS_RUNNING,),
        "next_status": None,
        "reject_message": "预演只允许运行中的流程，草稿流程不能进入预演",
        "stage": "预演",
    },
    "执行编排": {
        "allowed_statuses": (STATUS_RUNNING,),
        "next_status": None,
        "reject_message": "真实执行只允许运行中的流程",
        "stage": "执行",
    },
    "回滚流程": {
        "allowed_statuses": (STATUS_RUNNING,),
        "next_status": STATUS_ROLLED_BACK,
        "reject_message": "回滚只允许运行中的失败流程，已完成流程不能再次回滚",
        "stage": "回滚",
    },
    "归档流程": {
        "allowed_statuses": (STATUS_COMPLETED,),
        "next_status": STATUS_COMPLETED,
        "reject_message": "归档只允许已完成的流程",
        "stage": "归档",
    },
}

# 共用规则表：预演判断不再散落在各分支里，超时、并行、资源争用与回滚条件
# 统一从这里读取，并由下面同一份判定实现执行。
_RULE_TABLE: list[dict[str, Any]] = [
    {
        "id": "ORCH-GATE-001",
        "name": "状态门禁",
        "stages": ["启动", "预演", "执行", "回滚", "归档"],
        "condition": "流程状态不在动作允许的状态集合内",
        "outcome": "拒绝动作并返回可读原因",
        "source": "流程.status",
    },
    {
        "id": "ORCH-PARALLEL-001",
        "name": "并行分组预编译",
        "stages": ["预演", "执行"],
        "condition": "并行分组必须在判定开始前一次性编译，禁止在步骤超时后补判",
        "outcome": "输出固定的并行分组与组内步骤",
        "source": "流程.并行策略",
    },
    {
        "id": "ORCH-TIMEOUT-001",
        "name": "步骤超时",
        "stages": ["预演", "执行"],
        "condition": "步骤预估耗时 > 超时上限（默认 90 秒）",
        "outcome": "标记步骤已超时，流程判为需回滚",
        "source": "流程.超时控制 + 步骤.estimated_seconds",
    },
    {
        "id": "ORCH-RESOURCE-001",
        "name": "资源争用优先级裁决",
        "stages": ["预演", "执行"],
        "condition": "同一并行分组内多个分支争用同一资源",
        "outcome": "优先级数值最小的分支可执行，其余分支阻塞",
        "source": "步骤.resource + 步骤.priority",
    },
    {
        "id": "ORCH-DEPEND-001",
        "name": "上游级联阻塞",
        "stages": ["预演", "执行"],
        "condition": "上游步骤阻塞或超时",
        "outcome": "下游依赖步骤级联阻塞，不进入可执行清单",
        "source": "步骤.depends_on",
    },
    {
        "id": "ORCH-ROLLBACK-001",
        "name": "回滚读取执行快照",
        "stages": ["回滚"],
        "condition": "回滚必须基于执行时固化的 plan_snapshot",
        "outcome": "忽略当前编排步骤的后续变更，按快照输出补偿清单",
        "source": "流程.plan_snapshot",
    },
    {
        "id": "ORCH-ROLLBACK-002",
        "name": "终态不可再次回滚",
        "stages": ["回滚"],
        "condition": "流程已完成或已回滚，或没有执行快照",
        "outcome": "拒绝回滚并返回可读原因",
        "source": "流程.status + 流程.plan_snapshot",
    },
    {
        "id": "ORCH-LIMITER-001",
        "name": "降级引用同一规则版本",
        "stages": ["限流降级"],
        "condition": "限流降级拿到编排结果时，其 rule_version 必须与当前规则版本一致",
        "outcome": "版本一致才按编排结果裁决降级，否则拒绝降级",
        "source": "编排结果.rule_version",
    },
]


def rule_entries(stage: str | None = None) -> list[dict[str, Any]]:
    """返回规则表；可按阶段过滤，预演、执行、回滚、限流降级共用同一份条目。"""
    if stage is None:
        return [deepcopy(entry) for entry in _RULE_TABLE]
    return [
        deepcopy(entry)
        for entry in _RULE_TABLE
        if stage in entry["stages"]
    ]


def rules_document(stage: str | None = None) -> dict[str, Any]:
    """规则版本的对外说明，接口层直接返回该结构。"""
    return {
        "rule_version": RULE_VERSION,
        "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "stage": stage or "all",
        "entries": rule_entries(stage),
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_digest(snapshot: dict[str, Any]) -> str:
    """对快照做稳定摘要：当前编排步骤变化时，回滚仍能以摘要识别旧快照。"""
    return hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()


def _read_field(raw: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return default


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit() or ch == "-")
    try:
        return int(float(digits)) if digits else default
    except ValueError:
        return default


def _as_priority(value: Any, default: int = 100) -> int:
    """优先级数值越小越先执行；“高/中/低”映射为 1/2/3。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value or "").strip()
    mapped = {"高": 1, "中": 2, "低": 3, "最高": 0, "最低": 9}
    if text in mapped:
        return mapped[text]
    return _as_int(text, default)


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"否", "false", "0", "no", "optional", "可选"}:
        return False
    if text in {"是", "true", "1", "yes", "required", "必需"}:
        return True
    return default


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return value


def _normalize_steps(raw: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    parsed = _parse_jsonish(raw)
    if isinstance(parsed, dict) and "steps" in parsed:
        parsed = parsed["steps"]
    if not isinstance(parsed, list) or not parsed:
        return None, "编排步骤为空或不是结构化步骤列表"

    steps: list[dict[str, Any]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            return None, f"第 {index} 个编排步骤不是对象"
        step_id = str(_read_field(item, "id", "步骤编号", default=f"S{index}")).strip()
        if not step_id:
            return None, f"第 {index} 个编排步骤缺少步骤编号"
        depends_on = _read_field(item, "depends_on", "依赖步骤", default=[]) or []
        if isinstance(depends_on, str):
            depends_on = [part.strip() for part in depends_on.replace("，", ",").split(",") if part.strip()]
        steps.append(
            {
                "id": step_id,
                "name": str(_read_field(item, "name", "步骤名称", default=step_id)),
                "group": str(_read_field(item, "group", "并行分组", default="")).strip(),
                "estimated_seconds": _as_int(
                    _read_field(item, "estimated_seconds", "预估耗时", "耗时", default=0), 0
                ),
                "resource": str(_read_field(item, "resource", "占用资源", default="")).strip(),
                "priority": _as_priority(_read_field(item, "priority", "优先级", default=100)),
                "depends_on": [str(dep).strip() for dep in depends_on if str(dep).strip()],
                "required": _as_bool(_read_field(item, "required", "是否必需", default=True)),
                "order": index,
            }
        )

    seen: set[str] = set()
    for step in steps:
        if step["id"] in seen:
            return None, f"编排步骤编号「{step['id']}」重复"
        seen.add(step["id"])
    for step in steps:
        missing = [dep for dep in step["depends_on"] if dep not in seen]
        if missing:
            return None, f"步骤 {step['id']} 依赖了不存在的步骤：{'、'.join(missing)}"
    return steps, None


def _normalize_groups(raw: Any, steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    parsed = _parse_jsonish(raw)
    groups: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        items = parsed.get("groups")
        parsed_mode = str(parsed.get("mode") or "并行")
        if isinstance(items, list):
            for index, item in enumerate(items, start=1):
                if isinstance(item, dict):
                    group_id = str(_read_field(item, "id", "分组编号", default=f"G{index}"))
                    step_ids = _read_field(item, "steps", "步骤", default=[]) or []
                    name = str(_read_field(item, "name", "分组名称", default=group_id))
                    mode = str(_read_field(item, "mode", "执行方式", default=parsed_mode))
                elif isinstance(item, list):
                    group_id, step_ids, name, mode = f"G{index}", item, f"G{index}", parsed_mode
                else:
                    continue
                groups.append(
                    {
                        "id": group_id,
                        "name": name,
                        "mode": mode,
                        "steps": [str(step_id) for step_id in step_ids],
                    }
                )
    elif isinstance(parsed, list):
        for index, item in enumerate(parsed, start=1):
            if isinstance(item, dict):
                groups.append(
                    {
                        "id": str(_read_field(item, "id", "分组编号", default=f"G{index}")),
                        "name": str(_read_field(item, "name", "分组名称", default=f"G{index}")),
                        "mode": str(_read_field(item, "mode", "执行方式", default="并行")),
                        "steps": [str(step_id) for step_id in (_read_field(item, "steps", "步骤", default=[]) or [])],
                    }
                )
            elif isinstance(item, list):
                groups.append({"id": f"G{index}", "name": f"G{index}", "mode": "并行", "steps": [str(s) for s in item]})

    if not groups:
        declared = sorted({step["group"] for step in steps if step["group"]}, key=lambda value: value)
        for group_id in declared:
            groups.append(
                {
                    "id": group_id,
                    "name": group_id,
                    "mode": "并行",
                    "steps": [step["id"] for step in steps if step["group"] == group_id],
                }
            )

    if not groups:
        groups = [
            {
                "id": "G1",
                "name": "默认分组",
                "mode": "串行",
                "steps": [step["id"] for step in steps],
            }
        ]

    known = {step["id"] for step in steps}
    covered: set[str] = set()
    for group in groups:
        if not isinstance(group["steps"], list):
            return [], f"并行分组 {group['id']} 的步骤列表格式不正确"
        unknown = [step_id for step_id in group["steps"] if step_id not in known]
        if unknown:
            return [], f"并行分组 {group['id']} 引用了不存在的步骤：{'、'.join(unknown)}"
        duplicated = [step_id for step_id in group["steps"] if group["steps"].count(step_id) > 1]
        if duplicated:
            return [], f"并行分组 {group['id']} 内步骤重复：{'、'.join(sorted(set(duplicated)))}"
        repeated = [step_id for step_id in group["steps"] if step_id in covered]
        if repeated:
            return [], f"步骤 {'、'.join(repeated)} 同时出现在多个并行分组"
        covered.update(group["steps"])

    leftovers = [step["id"] for step in steps if step["id"] not in covered]
    if leftovers:
        groups.append({"id": "G-其余", "name": "其余步骤", "mode": "串行", "steps": leftovers})
    return groups, None


def compile_plan(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """把编排步骤与并行策略编译成不可变快照；并行分组只在这里判定一次。"""
    steps, error = _normalize_steps(entry.get("编排步骤"))
    if error:
        return None, error
    groups, group_error = _normalize_groups(entry.get("并行策略"), steps or [])
    if group_error:
        return None, group_error
    snapshot = {
        "flow_code": str(entry.get("流程编号") or ""),
        "flow_name": str(entry.get("流程名称") or ""),
        "steps": steps,
        "parallel_policy": {
            "groups": groups,
            "resource_arbitration": "priority",
        },
    }
    return snapshot, None


def _group_index(groups: list[dict[str, Any]]) -> dict[str, str]:
    return {
        step_id: group["id"]
        for group in groups
        for step_id in group["steps"]
    }


def _step_view(step: dict[str, Any], group_id: str) -> dict[str, Any]:
    return {
        "id": step["id"],
        "name": step["name"],
        "group": group_id,
        "estimated_seconds": step["estimated_seconds"],
        "resource": step["resource"],
        "priority": step["priority"],
        "depends_on": list(step["depends_on"]),
        "required": step["required"],
    }


def _judge_plan(snapshot: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    """唯一判定实现：并行分组 -> 资源优先级 -> 超时 -> 依赖级联 -> 输出清单。

    并行分组来自编译好的快照，因此不会出现“步骤超时后才判断并行策略”的情况；
    资源争用先按优先级裁决，再进入超时与依赖判断。
    """
    steps: list[dict[str, Any]] = snapshot["steps"]
    groups: list[dict[str, Any]] = snapshot["parallel_policy"]["groups"]
    group_of = _group_index(groups)
    step_by_id = {step["id"]: step for step in steps}
    blocked: dict[str, dict[str, Any]] = {}
    timed_out: dict[str, dict[str, Any]] = {}

    def block(step: dict[str, Any], rule_id: str, reason: str, blocked_by: list[str]) -> None:
        if step["id"] in blocked or step["id"] in timed_out:
            return
        blocked[step["id"]] = {
            "id": step["id"],
            "name": step["name"],
            "group": group_of.get(step["id"], ""),
            "rule_id": rule_id,
            "reason": reason,
            "blocked_by": blocked_by,
            "required": step["required"],
        }

    # 1) 并行分组已在 compile_plan 中固定，这里只按分组做资源优先级裁决。
    for group in groups:
        if str(group.get("mode") or "").strip().lower() in {"串行", "serial", "sequential"}:
            # 串行分组本身按顺序执行，不存在两个分支同时争用资源。
            continue
        resources: dict[str, list[dict[str, Any]]] = {}
        for step_id in group["steps"]:
            step = step_by_id[step_id]
            if step["resource"]:
                resources.setdefault(step["resource"], []).append(step)
        for resource, contenders in resources.items():
            if len(contenders) < 2:
                continue
            winner = min(contenders, key=lambda item: (item["priority"], item["order"]))
            for contender in contenders:
                if contender["id"] == winner["id"]:
                    continue
                block(
                    contender,
                    "ORCH-RESOURCE-001",
                    f"资源「{resource}」优先裁决给步骤 {winner['id']}（优先级 {winner['priority']}）",
                    [winner["id"]],
                )

    # 2) 超时判断：争用失败的分支不再进入超时判断，优先级裁决先于超时。
    for step in steps:
        if step["id"] in blocked:
            continue
        if step["estimated_seconds"] > timeout_seconds:
            timed_out[step["id"]] = {
                "id": step["id"],
                "name": step["name"],
                "group": group_of.get(step["id"], ""),
                "estimated_seconds": step["estimated_seconds"],
                "timeout_seconds": timeout_seconds,
                "rule_id": "ORCH-TIMEOUT-001",
            }

    # 3) 依赖级联：上游阻塞或超时，下游一律阻塞。
    changed = True
    while changed:
        changed = False
        for step in steps:
            if step["id"] in blocked or step["id"] in timed_out:
                continue
            bad_deps = [
                dep
                for dep in step["depends_on"]
                if dep in blocked or dep in timed_out
            ]
            if bad_deps:
                block(
                    step,
                    "ORCH-DEPEND-001",
                    f"上游步骤 {'、'.join(bad_deps)} 未完成",
                    bad_deps,
                )
                changed = True

    executable: list[dict[str, Any]] = []
    for step in steps:
        if step["id"] in blocked or step["id"] in timed_out:
            continue
        executable.append(_step_view(step, group_of.get(step["id"], "")))

    required_steps = [step for step in steps if step["required"]]
    required_timed_out = [item for item in timed_out.values() if step_by_id[item["id"]]["required"]]
    # 资源争用的落败分支属于预期的调度裁决结果，本身不判失败；只有超时和
    # 上游失败引发的级联阻塞才让必需步骤失败。
    required_failure_blocked = [
        item
        for item in blocked.values()
        if item["required"] and item["rule_id"] != "ORCH-RESOURCE-001"
    ]
    arbitration_blocked = [
        item for item in blocked.values() if item["rule_id"] == "ORCH-RESOURCE-001"
    ]

    if not required_steps:
        outcome = OUTCOME_INVALID
        message = "没有必需步骤可供执行"
    elif required_timed_out:
        outcome = OUTCOME_NEEDS_ROLLBACK
        message = (
            f"预演判定：{len(executable)} 步可执行，{len(blocked)} 步阻塞，"
            f"{len(timed_out)} 步超过 {timeout_seconds} 秒超时，需回滚"
        )
    elif required_failure_blocked:
        outcome = OUTCOME_PARTIAL_BLOCKED if executable else OUTCOME_ALL_BLOCKED
        message = f"预演判定：{len(executable)} 步可执行，{len(blocked)} 步阻塞"
    else:
        outcome = OUTCOME_ARBITRATED if arbitration_blocked else EXECUTED_OUTCOME
        message = f"预演判定：{len(executable)} 步可执行"
        if arbitration_blocked:
            message += f"，{len(arbitration_blocked)} 步因资源优先级裁决让行"
        else:
            message += "，无阻塞"

    checklist = []
    for step in steps:
        item = _step_view(step, group_of.get(step["id"], ""))
        if step["id"] in timed_out:
            item["status"] = "已超时"
            item["reason"] = f"预估耗时 {step['estimated_seconds']} 秒，超过 {timeout_seconds} 秒上限"
            item["rule_id"] = "ORCH-TIMEOUT-001"
        elif step["id"] in blocked:
            item["status"] = "阻塞"
            item["reason"] = blocked[step["id"]]["reason"]
            item["rule_id"] = blocked[step["id"]]["rule_id"]
        else:
            item["status"] = "可执行"
            item["reason"] = ""
            item["rule_id"] = ""
        checklist.append(item)

    hits = ["ORCH-PARALLEL-001"]
    if timed_out:
        hits.append("ORCH-TIMEOUT-001")
    if any(item["rule_id"] == "ORCH-RESOURCE-001" for item in blocked.values()):
        hits.append("ORCH-RESOURCE-001")
    if any(item["rule_id"] == "ORCH-DEPEND-001" for item in blocked.values()):
        hits.append("ORCH-DEPEND-001")

    return {
        "outcome": outcome,
        "message": message,
        "timeout_seconds": timeout_seconds,
        "parallel_groups": [
            {
                "id": group["id"],
                "name": group["name"],
                "mode": group["mode"],
                "steps": list(group["steps"]),
            }
            for group in groups
        ],
        "group_count": len(groups),
        "executable_steps": executable,
        "blocked_steps": list(blocked.values()),
        "timed_out_steps": list(timed_out.values()),
        "checklist": checklist,
        "rule_hits": hits,
        "must_rollback": bool(required_timed_out or required_failure_blocked),
    }


def _base_report(entry: dict[str, Any], action: str, stage: str) -> dict[str, Any]:
    return {
        "ok": False,
        "allowed": False,
        "flow_code": str(entry.get("流程编号") or ""),
        "flow_name": str(entry.get("流程名称") or ""),
        "status_before": entry.get("status"),
        "stage": stage,
        "action": action,
        "rule_version": RULE_VERSION,
        "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    }


def _gate(entry: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    rule = ACTION_STATE_RULES.get(action)
    base = _base_report(entry, action, rule["stage"] if rule else "未知")
    if rule is None:
        base.update(
            {
                "outcome": OUTCOME_REJECTED,
                "message": f"动作「{action}」不属于服务编排组合可执行范围",
                "rule_hits": [],
            }
        )
        return base, None
    if entry.get("status") not in rule["allowed_statuses"]:
        base.update(
            {
                "outcome": OUTCOME_REJECTED,
                "message": rule["reject_message"],
                "rule_hits": ["ORCH-GATE-001"],
            }
        )
        return base, None
    return None, rule


def _evaluate_plan(entry: dict[str, Any], action: str, timeout_seconds: int | None) -> dict[str, Any]:
    gate_report, rule = _gate(entry, action)
    if gate_report is not None:
        return gate_report
    assert rule is not None
    report = _base_report(entry, action, rule["stage"])
    report["allowed"] = True
    limit = DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else int(timeout_seconds)
    if limit <= 0:
        limit = DEFAULT_TIMEOUT_SECONDS

    snapshot, error = compile_plan(entry)
    if error:
        report.update(
            {
                "outcome": OUTCOME_INVALID,
                "message": error,
                "rule_hits": ["ORCH-PARALLEL-001"],
            }
        )
        return report

    assert snapshot is not None
    verdict = _judge_plan(snapshot, limit)
    report.update(verdict)
    report["plan_snapshot"] = deepcopy(snapshot)
    report["plan_digest"] = plan_digest(snapshot)
    report["ok"] = True
    return report


def _evaluate_transition(entry: dict[str, Any], action: str) -> dict[str, Any]:
    gate_report, rule = _gate(entry, action)
    if gate_report is not None:
        return gate_report
    assert rule is not None
    report = _base_report(entry, action, rule["stage"])
    report.update(
        {
            "ok": True,
            "allowed": True,
            "outcome": OUTCOME_ACCEPTED,
            "message": f"编排流程已{action}",
            "status_after": rule["next_status"],
            "rule_hits": ["ORCH-GATE-001"],
        }
    )
    return report


def _evaluate_rollback(entry: dict[str, Any]) -> dict[str, Any]:
    gate_report, rule = _gate(entry, "回滚流程")
    if gate_report is not None:
        return gate_report
    assert rule is not None
    report = _base_report(entry, "回滚流程", "回滚")
    report["allowed"] = True

    snapshot = entry.get("plan_snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("steps"):
        report.update(
            {
                "outcome": OUTCOME_REJECTED,
                "message": "回滚必须读取执行快照，当前流程没有可用的 plan_snapshot",
                "rule_hits": ["ORCH-ROLLBACK-002"],
            }
        )
        return report

    snapshot = deepcopy(snapshot)
    snapshot_digest = str(entry.get("plan_digest") or plan_digest(snapshot))
    current_snapshot, current_error = compile_plan(entry)
    current_digest = plan_digest(current_snapshot) if current_snapshot else ""
    differs = bool(current_error or (current_digest and current_digest != snapshot_digest))

    last_execution = entry.get("last_execution") if isinstance(entry.get("last_execution"), dict) else {}
    executed_ids = [str(step_id) for step_id in (last_execution.get("executed_steps") or [])]
    snapshot_steps = snapshot["steps"]
    step_by_id = {step["id"]: step for step in snapshot_steps}
    group_of = _group_index(snapshot["parallel_policy"]["groups"])

    rollback_steps: list[dict[str, Any]] = []
    for step_id in reversed(executed_ids):
        step = step_by_id.get(step_id)
        if step is not None:
            rollback_steps.append(_step_view(step, group_of.get(step_id, "")))

    blocked_rollback: list[dict[str, Any]] = []
    for step in snapshot_steps:
        if step["id"] in executed_ids:
            continue
        blocked_rollback.append(
            {
                "id": step["id"],
                "name": step["name"],
                "group": group_of.get(step["id"], ""),
                "rule_id": "ORCH-ROLLBACK-001",
                "reason": "该步骤未执行，无需补偿，不进入回滚可执行清单",
                "required": step["required"],
            }
        )

    report.update(
        {
            "ok": True,
            "outcome": STATUS_ROLLED_BACK if rollback_steps else "无可回滚步骤",
            "message": (
                f"按执行快照回滚：{len(rollback_steps)} 步可补偿，"
                f"{len(blocked_rollback)} 步无需补偿"
            ),
            "plan_snapshot": snapshot,
            "plan_digest": snapshot_digest,
            "snapshot_source": "执行快照 plan_snapshot",
            "snapshot_differs_from_current": differs,
            "current_plan_digest": current_digest,
            "current_plan_error": current_error or "",
            "executable_steps": rollback_steps,
            "blocked_steps": blocked_rollback,
            "rollback_order": [step["id"] for step in rollback_steps],
            "rule_hits": ["ORCH-ROLLBACK-001"],
        }
    )
    if differs:
        report["message"] += "；当前编排步骤与执行快照不一致，已按执行快照处理"
    return report


def evaluate_action(
    entry: dict[str, Any],
    action: str,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """预演、真实执行、回滚统一入口；三者读取同一 rule_version 与同一规则表。"""
    if action in {"预演编排", "执行编排"}:
        return _evaluate_plan(entry, action, timeout_seconds)
    if action == "回滚流程":
        return _evaluate_rollback(entry)
    return _evaluate_transition(entry, action)


def build_execution_log(report: dict[str, Any]) -> dict[str, Any]:
    """生成一条自洽执行日志：流程级 outcome 只有一个，不会同时写已完成与失败。"""
    outcome = str(report.get("outcome") or "")
    failed = outcome in FAILURE_OUTCOMES or outcome == OUTCOME_INVALID
    completed = outcome == EXECUTED_OUTCOME or outcome == OUTCOME_ARBITRATED
    return {
        "stage": "执行",
        "outcome": outcome,
        "completed": completed,
        "failed": failed,
        "rule_version": report.get("rule_version", RULE_VERSION),
        "plan_digest": report.get("plan_digest", ""),
        "timeout_seconds": report.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        "executed_steps": [step["id"] for step in report.get("executable_steps", [])],
        "timed_out_steps": [step["id"] for step in report.get("timed_out_steps", [])],
        "blocked_steps": [step["id"] for step in report.get("blocked_steps", [])],
        "message": report.get("message", ""),
    }


def normalize_execution_log(value: Any) -> list[dict[str, Any]]:
    """把历史日志规整为单结果事件，修复“已完成与失败并存”的旧记录。"""
    parsed = value if isinstance(value, list) else _parse_jsonish(value)
    if not isinstance(parsed, list):
        if value in (None, "", [], {}):
            return []
        return [{"stage": "历史", "outcome": "未结构化", "raw": str(value)}]

    events: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        event = deepcopy(item)
        failed_steps = event.get("failed_steps") or event.get("timed_out_steps") or []
        completed_steps = event.get("completed_steps") or event.get("executed_steps") or []
        if event.get("outcome") == STATUS_COMPLETED and failed_steps:
            event["outcome"] = "失败"
        event["completed"] = bool(event.get("completed")) and not event.get("failed")
        event["failed"] = bool(event.get("failed")) or bool(failed_steps)
        if event["failed"]:
            event["completed"] = False
        if event["completed"]:
            event["failed"] = False
        event.setdefault("executed_steps", list(completed_steps))
        events.append(event)
    return events


def log_is_consistent(events: list[dict[str, Any]]) -> bool:
    """检查日志内不存在同一事件既标记已完成又标记失败。"""
    return all(not (event.get("completed") and event.get("failed")) for event in events)


def limiter_degrade_decision(report: dict[str, Any] | None) -> dict[str, Any]:
    """调用限流降级引用同一规则版本裁决降级；版本不一致时拒绝降级。"""
    if not isinstance(report, dict):
        return {
            "rule_version": RULE_VERSION,
            "rule_id": "ORCH-LIMITER-001",
            "decision": "拒绝降级",
            "reason": "缺少编排结果，无法按同一规则版本裁决",
        }
    if report.get("rule_version") != RULE_VERSION:
        return {
            "rule_version": RULE_VERSION,
            "rule_id": "ORCH-LIMITER-001",
            "decision": "拒绝降级",
            "reason": "编排结果使用的规则版本与当前版本不一致",
            "report_rule_version": report.get("rule_version"),
        }
    degrade = report.get("outcome") in FAILURE_OUTCOMES
    return {
        "rule_version": RULE_VERSION,
        "rule_id": "ORCH-LIMITER-001",
        "decision": "降级" if degrade else "保持",
        "reason": (
            f"编排结果「{report.get('outcome')}」要求降级"
            if degrade
            else f"编排结果「{report.get('outcome')}」无需降级"
        ),
        "flow_code": report.get("flow_code"),
        "outcome": report.get("outcome"),
        "executable_steps": [step["id"] for step in report.get("executable_steps", [])],
        "blocked_steps": [step["id"] for step in report.get("blocked_steps", [])],
    }
