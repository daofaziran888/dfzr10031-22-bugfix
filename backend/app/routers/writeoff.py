"""分析管道批量入账冲销接口：批次提交、失败复核重启、集市流水与借方台账查询。

挂在分析管道模块下（/api/pipeline/writeoff/...），业务规则全部在 services/writeoff.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.writeoff import WriteOffService

router = APIRouter(prefix="/api/pipeline/writeoff", tags=["分析管道-批量冲销"])

service = WriteOffService()


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    keyword: str | None = Query(default=None, description="按批次号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """冲销批次列表，最新提交在前。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_batches(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/batches", response_model=ActionResult)
def submit_batch(payload: EntryPayload) -> ActionResult:
    """提交一个冲销批次：逐行校验、争用分配、入账并回写集市；同批次号重复提交不新增抵扣。"""
    receipt, message = service.submit_batch(payload.values)
    if receipt is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=receipt)


@router.get("/batches/{batch_no}", response_model=dict)
def get_batch(batch_no: str) -> dict:
    """批次详情：含每条行回执与关联集市流水。"""
    detail = service.get_batch(batch_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"冲销批次 {batch_no} 不存在")
    return detail


@router.get("/failures", response_model=dict)
def list_failures(batch_no: str | None = Query(default=None, description="按批次号过滤")) -> dict:
    """失败项清单（已拒绝、待复核），每条带拒绝原因与可纠正标记，供复核多选。"""
    items = service.list_failures(batch_no)
    return {"items": items, "total": len(items)}


@router.post("/restart", response_model=ActionResult)
def restart_failures(payload: EntryPayload) -> ActionResult:
    """复核多选重新开始：只处理可纠正条目，可随请求带纠正值（结算日、金额、活动编号）。"""
    result, message = service.restart_failures(payload.values)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/mart-flows", response_model=dict)
def list_mart_flows() -> dict:
    """集市原流水视图：已确认净额与待补差额随批次入账回执更新。"""
    items = service.list_mart_flows()
    return {"items": items, "total": len(items)}


@router.get("/debit-ledger", response_model=dict)
def list_ledger() -> dict:
    """借方台账视图：原量、已确认冲销与剩余可冲量。"""
    items = service.list_ledger()
    return {"items": items, "total": len(items)}
