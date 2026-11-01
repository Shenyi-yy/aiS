"""Phase 13: 执行请求状态机 —— 创建/建议 → 人工批准或拒绝 → 白名单执行 → 落库。

流转图 (终态不可逆):

    PENDING_APPROVAL ──approve──→ 执行 Docker 白名单动作
    │                                ├─ 状态核验通过 → EXECUTED
    │                                └─ 通道/核验失败 → FAILED (引擎原文进 error)
    └──────────reject──────────────→ REJECTED

关键不变式 (验证脚本逐条对质):
1. 全系统只有 approve_request 会碰 docker_exec; 创建/建议路径零执行。
2. 非 PENDING_APPROVAL 再审批 → ExecConflict (409), 防"重复批准=重复执行"。
3. AUTO 建议按 (problem_record_id, op_id) 去重且含 REJECTED —— 人被拒了
   后台不许换个马甲再提同一件事。
4. EXEC_ENABLED=false 时批准直接 503, 状态留在 PENDING (不静默吞掉)。
5. EXECUTED 必须以执行后回读的容器状态为准 (restart→running), 诚实落库。
"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AIAnalysisRecord, ExecutionRecord, ProblemRecord
from app.services import audit, docker_exec, exec_catalog

logger = logging.getLogger("zops.services.execution")

TERMINAL_STATUSES = ("EXECUTED", "FAILED", "REJECTED")

# 各动作执行后的期望容器状态 (核验不过就落 FAILED)
EXPECTED_STATE = {"restart": "running", "start": "running", "stop": "exited"}


class ExecNotFound(Exception):
    """请求/操作/问题不存在 → 404。"""


class ExecConflict(Exception):
    """状态机违例 (重复批准/批准已终态的单) → 409。"""


class ExecDisabled(Exception):
    """EXEC_ENABLED=false, 执行通道关闭 → 503。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_request(db: Session, request_id: int) -> ExecutionRecord:
    row = db.get(ExecutionRecord, request_id)
    if row is None:
        raise ExecNotFound(f"执行请求 {request_id} 不存在")
    return row


def create_request(
    db: Session,
    op_id: str,
    problem_record_id: int | None = None,
    source: str = "MANUAL",
    analysis_id: int | None = None,
    proposal: str = "",
) -> ExecutionRecord:
    """创建一张 PENDING_APPROVAL 审批单 (本函数零执行副作用)。"""
    op = exec_catalog.get_operation(op_id)
    if op is None:
        raise ExecNotFound(f"操作 {op_id!r} 不在执行目录")
    if op not in exec_catalog.visible_operations():
        # 目录有定义但环境白名单没授权该容器: 拒绝创建, 错误里说清楚
        raise ExecConflict(
            f"操作 {op_id} 的目标 {op.target!r} 不在 EXEC_ALLOW_TARGETS 白名单, 不可执行"
        )
    problem: ProblemRecord | None = None
    if problem_record_id is not None:
        problem = db.get(ProblemRecord, problem_record_id)
        if problem is None:
            raise ExecNotFound(f"归档问题 {problem_record_id} 不存在")
    if analysis_id is not None and db.get(AIAnalysisRecord, analysis_id) is None:
        raise ExecNotFound(f"分析记录 {analysis_id} 不存在")

    req = ExecutionRecord(
        problem_record_id=problem.id if problem else None,
        analysis_id=analysis_id,
        op_id=op.op_id,
        action=op.action,
        target=op.target,
        host=problem.host if problem else "",
        source=source,
        status="PENDING_APPROVAL",
        proposal=proposal
        or f"手工申请执行目录操作: {op.name} ({op.action} {op.target})",
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    logger.info(
        "创建执行请求 id=%s op=%s source=%s problem=%s (待人工审批)",
        req.id, op.op_id, source, req.problem_record_id,
    )
    # Phase 14: 建议/请求生成入链 (含零执行承诺的起点)
    audit.record_event(
        db=db,
        event_type=audit.EV_EXEC_REQUEST_CREATED,
        actor="dispatcher" if source == "AUTO" else "api-client",
        source="AUTO" if source == "AUTO" else "API",
        object_type="execution_record", object_id=req.id,
        link_problem_id=req.problem_record_id,
        detail={
            "op_id": req.op_id, "action": req.action, "target": req.target,
            "request_source": source, "analysis_id": analysis_id,
            "proposal": req.proposal[:300], "status": req.status,
        },
    )
    return req


def suggest_for_problem(
    db: Session, problem: ProblemRecord, analysis: AIAnalysisRecord
) -> ExecutionRecord | None:
    """Phase 13 自动建议: 自动分析成功后按目录匹配生成审批单 (仍待批准)。

    由 ai_dispatcher 在 trigger_source=AUTO 且 SUCCESS 后调用 ——
    AI 的"建议"止步于此: 这里生成的记录离执行还隔着一道人工审批。
    按 (problem, op) 去重 (任何状态的旧单都算提过), 返回新建的单或 None。
    """
    settings = get_settings()
    if not settings.exec_auto_suggest or not settings.exec_enabled:
        return None
    created: ExecutionRecord | None = None
    for op in exec_catalog.match_for_problem(problem.trigger_name):
        existing = db.scalars(
            select(ExecutionRecord).where(
                ExecutionRecord.problem_record_id == problem.id,
                ExecutionRecord.op_id == op.op_id,
            )
        ).first()
        if existing is not None:
            continue  # 提过就不再生成 (含 REJECTED: 人的否决必须被尊重)
        req = create_request(
            db,
            op.op_id,
            problem_record_id=problem.id,
            source="AUTO",
            analysis_id=analysis.id,
            proposal=(
                f"后台自动建议: 触发器「{problem.trigger_name}」命中目录操作 "
                f"{op.name}; 依据分析记录 id={analysis.id} (trigger_source=AUTO)。"
                f"风险提示: {op.risk}"
            ),
        )
        created = req
    return created


def approve_request(
    db: Session, request_id: int, approver: str, comment: str = ""
) -> ExecutionRecord:
    """人工批准 = 唯一执行入口。同步执行并回读状态, 结果如实落库。"""
    req = get_request(db, request_id)
    if req.status != "PENDING_APPROVAL":
        raise ExecConflict(f"请求 {request_id} 已是 {req.status}, 终态不可再批准")
    if not get_settings().exec_enabled:
        # 通道关闭: 什么都不做, 请求留在 PENDING, 把开关状态还给调用方
        raise ExecDisabled("EXEC_ENABLED=false: 执行通道已关闭, 批准未生效")

    # 再次对质目录 (创建到批准之间白名单可能已收紧)
    op = exec_catalog.get_operation(req.op_id)
    if op is None or op not in exec_catalog.visible_operations():
        raise ExecConflict(f"操作 {req.op_id} 已不在可见白名单, 拒绝执行")

    req.approver = approver
    req.comment = comment
    req.decided_at = _now()
    started = time.monotonic()
    try:
        outcome = docker_exec.run_container_action(op.action, op.target)
        elapsed = int((time.monotonic() - started) * 1000)
        expected = EXPECTED_STATE.get(op.action, "running")
        req.elapsed_ms = elapsed
        req.result = (
            f"docker {op.action} {op.target}: HTTP {outcome['http_status']}, "
            f"回读状态 {outcome['state']}, 耗时 {elapsed}ms"
        )
        if outcome["state"] == expected:
            req.status = "EXECUTED"
            logger.info("执行请求 %s 已批准并执行: %s", req.id, req.result)
        else:
            req.status = "FAILED"
            req.error = f"执行后回读状态 {outcome['state']} != 期望 {expected}"
            logger.warning("执行请求 %s 状态核验失败: %s", req.id, req.error)
    except docker_exec.ExecError as exc:
        req.elapsed_ms = int((time.monotonic() - started) * 1000)
        req.status = "FAILED"
        req.error = str(exc)[:1000]
        logger.warning("执行请求 %s 失败: %s", req.id, exc)
    except Exception as exc:  # 兜底: 任何未知异常也不许裸 500 吞掉审批证据
        req.elapsed_ms = int((time.monotonic() - started) * 1000)
        req.status = "FAILED"
        req.error = f"{type(exc).__name__}: {exc}"[:1000]
        logger.exception("执行请求 %s 未知异常 (已如实落 FAILED)", req.id)
    db.commit()
    db.refresh(req)
    # Phase 14: 批准 = 全平台唯一执行入口, 审批人与执行结果必须入链
    # (EXECUTED/FAILED 都记: 失败执行同样是"谁在什么时候动了容器"的证据)
    audit.record_event(
        db=db,
        event_type=audit.EV_EXEC_APPROVED,
        actor=approver, source="API",
        object_type="execution_record", object_id=req.id,
        link_problem_id=req.problem_record_id,
        detail={
            "op_id": req.op_id, "action": req.action, "target": req.target,
            "request_status": req.status, "result": req.result[:300],
            "error": req.error[:300], "elapsed_ms": req.elapsed_ms,
            "comment": comment[:200],
        },
    )
    return req


def reject_request(
    db: Session, request_id: int, approver: str, reason: str = ""
) -> ExecutionRecord:
    """人工否决: 只改单据状态, 零执行; REJECTED 后不许再批准。"""
    req = get_request(db, request_id)
    if req.status != "PENDING_APPROVAL":
        raise ExecConflict(f"请求 {request_id} 已是 {req.status}, 无需/不可再拒绝")
    req.status = "REJECTED"
    req.approver = approver
    req.comment = reason
    req.decided_at = _now()
    db.commit()
    db.refresh(req)
    logger.info("执行请求 %s 已被 %s 否决: %s", req.id, approver, reason)
    # Phase 14: 否决同样入链 —— 人的"不做"决定与"做"决定同权重留痕
    audit.record_event(
        db=db,
        event_type=audit.EV_EXEC_REJECTED,
        actor=approver, source="API",
        object_type="execution_record", object_id=req.id,
        link_problem_id=req.problem_record_id,
        detail={
            "op_id": req.op_id, "action": req.action, "target": req.target,
            "request_status": req.status, "reason": reason[:300],
        },
    )
    return req


def pending_requests(db: Session) -> int:
    """待审批数 (GET /exec/config 与大盘用)。"""
    return len(
        db.scalars(
            select(ExecutionRecord.id).where(
                ExecutionRecord.status == "PENDING_APPROVAL"
            )
        ).all()
    )
