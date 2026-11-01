"""AI 分析路由: /api/v1/ai/* (Phase 9 LLM 接入)

- GET  /config            LLM 配置状态 (只回显 Key 长度, 绝不回显 Key)
- POST /check             一次真实最小往返, 验证供应商连通性与延迟
- POST /analyze/{problem_id}  对一条真实归档问题做故障分析
- GET  /tools             只读工具白名单回显 (Phase 10/11)
- POST /agent/{problem_id}    带工具取证的 Agent 分析 (trigger_source=MANUAL)
- GET  /auto              Phase 12: 自动分析调度状态 (配置+实时计数+待办数)
- GET  /analyses          分析归档分页 (含 FAILED 记录, 如实呈现)

LLM 与 Zabbix 调用全部收在 services (llm_client / zabbix_client),
路由层只做 HTTP 语义: 404 问题不存在 / 502 LLM 侧失败。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import AIAnalysisRecord
from app.schemas.analysis import (
    AnalysisOut,
    AnalysisPage,
    AutoStatus,
    CheckResult,
    LLMConfigOut,
    ToolInfo,
    ToolsList,
)
from app.schemas.common import ApiResponse
from app.services.ai_agent import analyze_problem_with_tools
from app.services.ai_analyzer import analyze_problem
from app.services.ai_dispatcher import STAT as DISPATCHER_STAT, pending_count
from app.services.llm_client import LLMError, get_default_llm_client
from app.services.ai_agent import ALL_TOOL_SCHEMAS

logger = logging.getLogger("zops.api.analysis")

router = APIRouter(prefix="/ai", tags=["ai"])


@router.get("/config", response_model=ApiResponse[LLMConfigOut])
def llm_config() -> ApiResponse[LLMConfigOut]:
    """LLM 三要素注入状态 (验证脚本据此判断 compose 是否生效)。"""
    settings = get_settings()
    client = get_default_llm_client()
    return ApiResponse(
        data=LLMConfigOut(
            configured=client.configured(),
            base_url=settings.llm_base_url or "(未配置)",
            model=settings.llm_model or "(未配置)",
            api_key_len=len(settings.llm_api_key),
            timeout_seconds=settings.llm_timeout,
        )
    )


@router.post("/check", response_model=ApiResponse[CheckResult])
def llm_check() -> ApiResponse[CheckResult]:
    """真实 ping: 失败原样 502, 不做任何假 OK。"""
    try:
        latency_ms = get_default_llm_client().ping()
    except LLMError as exc:
        logger.warning("LLM check 失败: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApiResponse(
        data=CheckResult(
            ok=True, model=get_settings().llm_model, latency_ms=latency_ms
        )
    )


@router.post("/analyze/{problem_id}", response_model=ApiResponse[AnalysisOut])
def analyze(
    problem_id: int,
    db: Session = Depends(get_db),
) -> ApiResponse[AnalysisOut]:
    """对 problem_records 里的一条真实告警跑 LLM 故障分析。

    大模型往返 10-60s 是常态, 调用方 (curl / 验证脚本) 请把超时放宽。
    """
    try:
        record = analyze_problem(db, problem_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMError as exc:
        # FAILED 记录已由 ai_analyzer 落库, 这里只负责把原因呈现出去
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApiResponse(data=AnalysisOut.model_validate(record))


@router.get("/tools", response_model=ApiResponse[ToolsList])
def list_tools() -> ApiResponse[ToolsList]:
    """Phase 10: 回显只读工具白名单 —— readonly 恒为 true, 是可核验的安全证据。"""
    tools = [
        ToolInfo(
            name=t["function"]["name"],
            description=t["function"]["description"],
            readonly=True,
        )
        for t in ALL_TOOL_SCHEMAS
    ]
    return ApiResponse(
        data=ToolsList(
            max_rounds=get_settings().llm_agent_max_rounds, tools=tools
        )
    )


@router.post("/agent/{problem_id}", response_model=ApiResponse[AnalysisOut])
def agent_analyze(
    problem_id: int,
    db: Session = Depends(get_db),
) -> ApiResponse[AnalysisOut]:
    """带 Zabbix 工具取证的 Agent 分析: 模型自主决定查什么, 轨迹落 tool_trace。

    多轮往返, 比 /analyze 更慢 (每轮都要等大模型) —— 调用方超时给足 10 分钟。
    """
    try:
        record = analyze_problem_with_tools(db, problem_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMError as exc:
        # FAILED 记录 (含已发生的工具轨迹) 已由 ai_agent 落库
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApiResponse(data=AnalysisOut.model_validate(record))


@router.get("/auto", response_model=ApiResponse[AutoStatus])
def auto_status(db: Session = Depends(get_db)) -> ApiResponse[AutoStatus]:
    """Phase 12: 自动分析调度状态 —— 配置/实时计数/待办数, 全部真实读取。"""
    settings = get_settings()
    snap = DISPATCHER_STAT.snapshot()
    return ApiResponse(
        data=AutoStatus(
            enabled=settings.ai_auto_enabled,
            running_now=bool(snap["running"]),
            interval_seconds=settings.ai_auto_interval,
            max_per_tick=settings.ai_auto_max_per_tick,
            max_rounds=settings.llm_agent_max_rounds,
            llm_configured=get_default_llm_client().configured(),
            model=settings.llm_model or "(未配置)",
            pending=pending_count(db),
            **{k: snap[k] for k in (
                "ticks", "last_tick_at", "last_scanned",
                "last_analyzed", "analyzed_total", "errors_total",
            )},
        )
    )


@router.get("/analyses", response_model=ApiResponse[AnalysisPage])
def list_analyses(
    db: Session = Depends(get_db),
    problem_record_id: int | None = Query(
        default=None, ge=1, description="按归档问题过滤"
    ),
    status: str | None = Query(
        default=None, pattern="^(SUCCESS|FAILED)$", description="按分析状态过滤"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[AnalysisPage]:
    """分页查询分析归档, 按时间倒序; FAILED 也在列表里 (诚实原则)。"""
    stmt = select(AIAnalysisRecord)
    if problem_record_id is not None:
        stmt = stmt.where(AIAnalysisRecord.problem_record_id == problem_record_id)
    if status is not None:
        stmt = stmt.where(AIAnalysisRecord.status == status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AIAnalysisRecord.created_at.desc()).offset(offset).limit(limit)
    ).all()

    page = AnalysisPage(
        total=total,
        offset=offset,
        limit=limit,
        items=[AnalysisOut.model_validate(r) for r in rows],
    )
    return ApiResponse(data=page)
