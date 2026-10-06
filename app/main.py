"""FastAPI 入口：HTTP API 与统一错误处理。"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import errors, service
from .auth import ROLE_APPROVE, ROLE_SUBMIT, authenticate
from .config import settings
from .db import init_db
from .errors import DomainError
from .schemas import (CorrectionBatch, CorrectionItem, CorrectionPreviewBatch,
                      CorrectionPreviewItem, CorrectionProposalCreate,
                      CoverageCheckRequest, PackageRequest,
                      ProposalDecisionRequest, ReleaseBatch, ReleaseItem,
                      WithdrawalBatch, WithdrawalItem)

app = FastAPI(
    title="安全作业规程历史回溯服务",
    version="1.1.0",
    description=("双时间维（适用时间 / 发布时间）规程版本库：首次发布、更正、"
                 "误发布更正的撤回（只追加事件）、按作业时刻+知悉时刻回溯查询、"
                 "两知悉时刻区间对比、作业规程包的固定/取回/复核、前置规程"
                 "清单的递归解析（单时刻闭包与跨时段作业区间的逐片段可用性）、"
                 "多条更正正式提交前的只读批量预演（不写历史、不占版本号）、"
                 "值班员凭本地凭证提交/审核待审更正提案（批准时在同一事务内"
                 "重验整批并写入原审计批次），以及调度员安排跨规程作业前的"
                 "版本覆盖检查（逐段整组状态）。"),
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.exception_handler(DomainError)
async def _domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status,
                        content={"error": {"code": exc.code,
                                           "message": exc.message,
                                           "details": exc.details}})


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request: Request,
                              exc: RequestValidationError) -> JSONResponse:
    # 包含额外字段（如试图回填的 published_at）、类型错误等
    return JSONResponse(status_code=422,
                        content={"error": {"code": "validation_error",
                                           "message": "请求参数校验失败",
                                           "details": {"errors": exc.errors()}}})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {"service": "sop-history",
            "docs": "/docs",
            "health": "/health",
            "endpoints": [
                "POST /api/v1/packages",
                "GET  /api/v1/packages/{package_id}",
                "GET  /api/v1/packages/{package_id}/review",
                "POST /api/v1/releases",
                "POST /api/v1/corrections",
                "POST /api/v1/corrections/preview",
                "POST /api/v1/withdrawals",
                "GET  /api/v1/effective",
                "GET  /api/v1/procedures/{number}/history",
                "GET  /api/v1/procedures/{number}/diff",
                "GET  /api/v1/procedures/{number}/closure",
                "GET  /api/v1/procedures/{number}/closure-plan",
                "POST /api/v1/coverage/check",
                "POST /api/v1/correction-proposals",
                "GET  /api/v1/correction-proposals/{proposal_id}",
                "POST /api/v1/correction-proposals/{proposal_id}/approve",
                "POST /api/v1/correction-proposals/{proposal_id}/reject",
                "GET  /api/v1/audit/events",
            ]}


# ------------------------------------------------------------ 值班员凭证鉴权


def _current_operator(
        authorization: str | None = Header(default=None)):
    """解析 Authorization: Bearer <token> 为本地配置的值班员身份。

    缺失/无法识别 -> 401 invalid_credentials；仅待审更正相关接口使用本依赖，
    既有直接更正/回溯/审计接口契约不变、无需凭证。
    """
    operator = authenticate(authorization, settings.operators)
    if operator is None:
        raise errors.invalid_credentials()
    return operator


def _require_role(required_role: str):
    def _dependency(operator=Depends(_current_operator)):
        if not operator.can(required_role):
            raise errors.insufficient_role(required_role)
        return operator
    return _dependency


def _different_reviewer(operator, submitted_by: str) -> None:
    """提交人与审核人不得相同（仅处理接口调用）。"""
    if operator.name == submitted_by:
        raise errors.reviewer_is_submitter(operator.name)


def _coerce_items(payload: dict, single_cls, batch_cls):
    """允许单条对象直接提交，或 {"items": [...]} 批量提交。"""
    try:
        if isinstance(payload, dict) and "items" in payload:
            return batch_cls(**payload).items
        return [single_cls(**payload)]
    except ValidationError as exc:
        # 手动校验与 FastAPI 入参校验走同一套错误响应
        raise RequestValidationError(exc.errors())


@app.post("/api/v1/releases", status_code=201)
async def create_release(request: Request) -> dict:
    """首次发布规程：编号、正文、UTC 适用区间 [起, 止)。"""
    payload = await request.json()
    items = _coerce_items(payload, ReleaseItem, ReleaseBatch)
    return service.publish_releases(items)


@app.post("/api/v1/corrections", status_code=201)
async def create_correction(request: Request) -> dict:
    """提交更正：编号、被替代版本、该版本当前生效区间内的子区间、新正文。

    服务端记录发布时间；批量提交时任一条目非法则整批拒绝。
    """
    payload = await request.json()
    items = _coerce_items(payload, CorrectionItem, CorrectionBatch)
    return service.submit_corrections(items)


@app.post("/api/v1/corrections/preview", status_code=200)
async def preview_correction(request: Request) -> dict:
    """更正批量预演：正式提交前按现有单条/批量更正字段预演整批结果。

    请求体支持单条对象（自身带 work_start/work_end），或
    ``{"work_start": ..., "work_end": ..., "items": [...]}`` 批量。
    预演在只读快照的内存副本上按与正式提交相同的规则逐项应用（同批按输入
    顺序、后项可引用前项拟生成版本），返回各项拟生成版本号、最终前置清单，
    以及指定非空作业区间内提交前后实际适用版本不同的最大片段。预演不写
    版本、不占版本号、不改变历史；任一项非法即指出位置（details.index）与
    现有错误原因，不返回部分成功结果。
    """
    payload = await request.json()
    items, work_start, work_end = _coerce_preview(payload)
    return service.preview_corrections(items, work_start, work_end)


def _coerce_preview(payload: dict) -> tuple[list, str, str]:
    """解析预演请求：单条对象（自带作业区间）或带 items 的批量对象。"""
    try:
        if isinstance(payload, dict) and "items" in payload:
            batch = CorrectionPreviewBatch(**payload)
            return batch.items, batch.work_start, batch.work_end
        single = CorrectionPreviewItem(**payload)
        return [single], single.work_start, single.work_end
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())


# ------------------------------------------------------- 待审更正提案


@app.post("/api/v1/correction-proposals", status_code=201)
async def create_proposal(req: CorrectionProposalCreate,
                          operator=Depends(_require_role(ROLE_SUBMIT))) -> dict:
    """值班员提交待审更正提案：``Authorization: Bearer <token>`` 出示本地
    配置且具 ``submit`` 角色的值班员身份。

    请求体 ``{"items": [...]}``，条目字段与 ``POST /api/v1/corrections``
    完全一致（至少 1 条）。服务保存**不可变提案**并返回编号、状态
    （``pending``）与条目；提交不写版本、**不占版本号**、不推进审计日志。
    401 凭证无效、403 缺少 submit 角色；额外字段 422。
    """
    return service.create_correction_proposal(req.items, operator)


@app.get("/api/v1/correction-proposals/{proposal_id}")
def get_proposal(proposal_id: str,
                 operator=Depends(_current_operator)) -> dict:
    """按编号查询待审更正提案（任一有效值班员身份即可）。

    返回提案内容（不可变条目快照）、状态、提交人/提交时刻、处理人/处理时刻
    与说明，以及批准成功后发布的版本（published_items）与发布时刻/审计批次；
    pending/rejected 时发布字段为 null。提案不存在 -> 404 proposal_not_found。
    """
    return service.get_correction_proposal(proposal_id)


async def _decision_payload(request: Request) -> dict:
    """批准/拒绝允许空请求体、空对象或带可选 reason 的对象。"""
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        return ProposalDecisionRequest.model_validate_json(raw).model_dump()
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())


@app.post("/api/v1/correction-proposals/{proposal_id}/approve", status_code=200)
async def approve_proposal(proposal_id: str, request: Request,
                           operator=Depends(_require_role(ROLE_APPROVE))) -> dict:
    """批准待审更正提案：须由具 ``approve`` 角色且**不同于提交人**的值班员
    调用（``Authorization: Bearer <token>``）。

    在同一写事务内按**当前版本链**重验整批基版、子区间、前置清单与同批顺序，
    通过后发布版本并写入原有审计批次，提案置为 approved；期间版本变化导致
    任一条非法时返回现有冲突原因（``details.index`` 为条目位置），提案仍
    pending，不留部分版本或审计事件。已处理提案重复处理（含批准已拒绝提案）
    -> 409 proposal_already_decided；并发批准至多成功一次。
    """
    payload = await _decision_payload(request)
    # 提交人≠审核人在读快照外先判定（事务内仍以留档的 submitted_by 复核身份）。
    existing = service.get_correction_proposal(proposal_id)
    _different_reviewer(operator, existing["submitted_by"])
    return service.decide_correction_proposal(
        proposal_id, operator, "approved", payload.get("reason"))


@app.post("/api/v1/correction-proposals/{proposal_id}/reject", status_code=200)
async def reject_proposal(proposal_id: str, request: Request,
                          operator=Depends(_require_role(ROLE_APPROVE))) -> dict:
    """拒绝待审更正提案：同样须由具 ``approve`` 角色且不同于提交人的值班员
    调用。请求体可空，或 ``{"reason": "..."}`` 说明拒绝原因。

    拒绝只更新提案状态（不写版本、不写审计）；已拒绝是终态，之后不可批准。
    已处理提案重复处理 -> 409 proposal_already_decided、状态不变。
    """
    payload = await _decision_payload(request)
    existing = service.get_correction_proposal(proposal_id)
    _different_reviewer(operator, existing["submitted_by"])
    return service.decide_correction_proposal(
        proposal_id, operator, "rejected", payload.get("reason"))


@app.post("/api/v1/withdrawals", status_code=201)
async def create_withdrawal(request: Request) -> dict:
    """撤回误发布的更正：编号 + 更正版本号。

    撤回时刻由服务端记录（不接受回填）；只追加撤回事件、不删除原版本。
    首版、已撤回版本、已有子更正的版本一律拒绝；批量提交时任一条非法整批回滚。
    """
    payload = await request.json()
    items = _coerce_items(payload, WithdrawalItem, WithdrawalBatch)
    return service.withdraw_corrections(items)


@app.get("/api/v1/effective")
def effective(work_time: str, known_as_of: str | None = None) -> dict:
    """回溯查询：截至 known_as_of 已发布、且在 work_time 有效的正文与版本。"""
    return service.query_effective(work_time, known_as_of)


@app.get("/api/v1/procedures/{number}/history")
def procedure_history(number: str) -> dict:
    """审计视图：某规程的全部版本（含尚未对旧知悉时刻可见的版本）。"""
    return service.list_history(number)


@app.get("/api/v1/procedures/{number}/diff")
def procedure_diff(number: str, known_before: str, known_after: str,
                   work_start: str, work_end: str) -> dict:
    """两知悉时刻对比：同一规程在作业区间 [work_start, work_end) 内，
    先后两个知悉时刻可见的实际适用正文发生变化的最大片段。

    时间均须为 ISO-8601 带时区；known_before 必须严格早于 known_after；
    作业区间非空。编号不存在返回 404，其余非法入参返回 422；均不写历史。
    """
    return service.compare_interval(number, known_before, known_after,
                                    work_start, work_end)


@app.get("/api/v1/procedures/{number}/closure")
def procedure_closure(number: str, work_time: str,
                      known_as_of: str | None = None) -> dict:
    """前置规程清单：班组按根规程准备作业时，连同该时点有效的前置规程核对。

    按现有回溯语义在同一读取快照内递归解析：每个规程取 (作业时刻, 知悉时刻)
    下的适用版本，并沿该版本携带的前置编号列表展开；返回去重后的根及前置
    规程清单（前置优先、根在最后，含编号/版本/正文）。根或引用编号不存在、
    任一规程当时无适用正文 -> 说明缺口并整体失败；引用成环 -> 返回环路径。
    """
    return service.resolve_closure(number, work_time, known_as_of)


@app.get("/api/v1/procedures/{number}/closure-plan")
def procedure_closure_plan(number: str, work_start: str, work_end: str,
                           known_as_of: str | None = None) -> dict:
    """跨时段前置规程清单：班组计划跨时段作业时，判断根规程及其递归前置在
    整个非空作业区间 [work_start, work_end) 内是否持续可用。

    在同一读取快照内按现有回溯与前置关系语义找出根及递归引用的版本切换
    边界，切成覆盖全区间、互不重叠的片段；每片段返回前置优先的去重清单及
    版本。根/引用缺失、无适用正文或引用成环时保留该片段并以缺口链/环路径
    标记（该片段与整体结论均为不可用）；相邻片段仅在清单与错误状态完全
    相同时合并，区间外的变动不影响结果。非法区间或时间直接 422、不写历史。
    """
    return service.resolve_closure_plan(number, work_start, work_end,
                                        known_as_of)


@app.post("/api/v1/packages", status_code=201)
async def create_package(req: PackageRequest) -> dict:
    """固定作业规程包：作业时刻 + 知悉时刻 + 一组规程编号。

    编号重复、缺失，或任一规程在该作业时刻/知悉时刻下无适用正文，均整体
    拒绝、不留包；知悉时刻不得晚于服务端当前时刻。成功返回包标识、各规程
    当时适用的版本与正文及两种时刻；包一经形成即不可变。
    """
    return service.create_package(req.work_time, req.known_as_of, req.numbers)


@app.get("/api/v1/packages/{package_id}")
def get_package(package_id: str) -> dict:
    """按标识取回作业规程包：返回固定时原样快照（含正文），之后的更正、
    撤回均不改变该内容。包不存在返回 404 package_not_found。"""
    return service.get_package(package_id)


@app.get("/api/v1/packages/{package_id}/review")
def review_package(package_id: str, known_as_of: str) -> dict:
    """复核作业规程包：以不早于包内知悉时刻、不晚于服务端当前时刻的复核
    知悉时刻，按原作业时刻逐项返回包内版本（pinned）、彼时有效版本
    （current）及 changed，并给出整体 all_match。

    包不存在 -> 404；复核时间非法（含早于包内知悉时刻、晚于当前时刻、
    缺时区）-> 422。
    """
    return service.review_package(package_id, known_as_of)


@app.post("/api/v1/coverage/check", status_code=200)
def coverage_check(req: CoverageCheckRequest) -> dict:
    """跨规程版本覆盖检查：调度员安排跨规程作业前核对版本覆盖。

    输入非空且不重复的规程编号、非空作业区间 [work_start, work_end) 与
    知悉时刻（不得晚于服务端当前时刻）；在同一只读快照内按全体编号的
    版本端点切分作业区间，逐段返回每个编号的有效版本号、正文或缺口，
    并标出因撤回导致的恢复、区间外空档与版本切换；相邻段仅在整组结果
    完全相同时合并。编号不存在 -> 404；时间非法、编号重复、知悉时刻
    晚于当前时刻 -> 422。全程只读、不写历史。
    """
    return service.check_coverage(req.numbers, req.work_start, req.work_end,
                                  req.known_as_of)


@app.get("/api/v1/audit/events")
def audit_events(cursor: str | None = None,
                 limit: str | None = None) -> dict:
    """值班审计变动日志：按提交顺序增量取回全站规程变动。

    查询参数：``cursor`` 为上一页响应的 ``next_cursor``（省略/空/0 表示
    起始游标，从升级前历史的第一条事件开始）；``limit`` 为每页事件数上限
    （默认 100、最大 1000）。返回事件列表、``next_cursor`` 与 ``has_more``。
    事件含操作类型（release/correction/withdrawal）、规程编号、版本号与
    服务端时刻；同一新提交批次按请求顺序排列且分页不可拆开，游标只随已提交
    事务前进。非法游标 -> 422 invalid_cursor；上限装不下下一完整批次 ->
    422 page_limit_too_small（游标不前进）；上限非 1..1000 整数 ->
    422 invalid_page_limit。
    """
    return service.read_change_feed(cursor, limit)
