"""服务编排组合接口：维护编排流程，覆盖预演、执行、回滚、启动与归档。

预演、真实执行和回滚都调用 ``OrchestraService.perform``，因此三个入口使用同一
规则版本与同一套规则表；规则表本身通过 ``GET /api/orchestra/rules`` 暴露。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, OrchestrationRunPayload, PageResult
from app.services.orchestra import OrchestraService

router = APIRouter(prefix="/api/orchestra", tags=["服务编排组合"])

service = OrchestraService()

LIST_FIELDS = ["流程编号", "流程名称", "编排步骤", "并行策略", "超时控制", "失败回滚", "执行日志", "流程状态"]
STATUSES = ["草稿", "运行中", "已完成", "已回滚"]


def _run(entry_id: int, action: str, payload: OrchestrationRunPayload | None = None) -> ActionResult:
    values = payload.values if payload is not None else {}
    timeout_raw = values.get("timeout_seconds", values.get("超时秒数"))
    timeout = timeout_raw if timeout_raw is not None else (payload.timeout_seconds if payload else None)
    if isinstance(timeout, str) and timeout.strip().isdigit():
        timeout = int(timeout.strip())
    entry, message, report = service.perform(entry_id, action, timeout_seconds=timeout)
    return ActionResult(ok=bool(report.get("ok")), message=message, entry=entry, result=report)


# 静态路径必须放在 /{entry_id} 之前，否则 export/rules 会被当成流程 id。
@router.get("/rules")
def get_rules(stage: str | None = Query(default=None, description="预演、执行、回滚、限流降级")) -> dict[str, Any]:
    """返回预演、执行、回滚与限流降级共用的规则表及规则版本。"""
    return service.rules(stage)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出服务编排组合清单：返回当前全量数据，含执行日志一致性标记。"""
    items, total = service.list_entries(page=1, size=10000)
    return {
        "module": "orchestra",
        "rule_version": service.rules()["rule_version"],
        "total": total,
        "items": items,
        "checklist": [
            {
                "id": item.get("id"),
                "流程编号": item.get("流程编号"),
                "流程状态": item.get("流程状态"),
                "执行日志": item.get("执行日志"),
                "执行日志一致": item.get("执行日志一致"),
                "rule_version": item.get("rule_version"),
            }
            for item in items
        ],
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按流程编号或流程名称检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已完成、已回滚"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按流程编号与状态过滤服务编排组合列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条编排流程，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="编排流程已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条编排流程明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"编排流程 {entry_id} 不存在或已归档")
    return entry


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改编排步骤或并行策略；终态流程不允许再改，避免回滚读到新旧混合的步骤。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/dry-run", response_model=ActionResult)
def dry_run(entry_id: int, payload: OrchestrationRunPayload | None = None) -> ActionResult:
    """预演编排：只读判定，输出可执行步骤与阻塞步骤；草稿流程不允许预演。"""
    return _run(entry_id, "预演编排", payload)


@router.post("/{entry_id}/execute", response_model=ActionResult)
def execute(entry_id: int, payload: OrchestrationRunPayload | None = None) -> ActionResult:
    """真实执行：与预演共用同一规则版本，执行时固化 plan_snapshot 供回滚读取。"""
    return _run(entry_id, "执行编排", payload)


@router.post("/{entry_id}/rollback", response_model=ActionResult)
def rollback(entry_id: int, payload: OrchestrationRunPayload | None = None) -> ActionResult:
    """失败回滚：只读取执行时固化的 plan_snapshot，已完成流程不能再次回滚。"""
    return _run(entry_id, "回滚流程", payload)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """兼容入口：按动作名执行启动编排、预演编排、执行编排、回滚流程、归档流程。"""
    action = str(payload.values.get("action") or "").strip()
    timeout_raw = payload.values.get("timeout_seconds", payload.values.get("超时秒数"))
    if isinstance(timeout_raw, str) and timeout_raw.strip().isdigit():
        timeout_raw = int(timeout_raw.strip())
    entry, message, report = service.perform(entry_id, action, timeout_seconds=timeout_raw)
    return ActionResult(ok=bool(report.get("ok")), message=message, entry=entry, result=report)
