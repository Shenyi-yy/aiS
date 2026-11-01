"""Phase 12: AI 自动故障分析调度 —— 新告警归档后模型自动取证分析。

闭环位置 (16 Phase 主线的"取数→AI分析"自动化段):

    Zabbix 告警 → (P5 归档循环) problem_records ACTIVE
      → 本模块周期扫描 "ACTIVE 且尚无分析记录" 的归档
      → analyze_problem_with_tools(trigger_source="AUTO")
      → 人工复盘 / (P13) 人工确认执行

设计约束:
- **同一问题只自动分析一次**: 以"已有分析记录 (SUCCESS 或 FAILED)"为跳过
  条件。FAILED 不无限重试 —— 失败原因进库, 要不要再来一次是人的决定
  (手工 POST /ai/agent/{id} 仍然随时可用, 记 MANUAL)。
- 单轮限量 AI_AUTO_MAX_PER_TICK (默认 3): 告警风暴时不许把 LLM 额度烧穿。
- 任何一轮异常只记日志, 下一轮继续 (与归档循环同款容错, 调度不能拖垮 API)。
- LLM 未配置时直接跳过 (不造假状态)。
"""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import AIAnalysisRecord, ProblemRecord
from app.services import audit
from app.services.ai_agent import analyze_problem_with_tools
from app.services.execution import suggest_for_problem
from app.services.llm_client import LLMError, get_default_llm_client

logger = logging.getLogger("zops.services.ai_dispatcher")


@dataclass
class DispatcherStat:
    """进程内运行计数 (GET /ai/auto 直接回显; 持久化审计是 P14 的事)。"""

    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    ticks: int = 0
    running: bool = False
    last_tick_at: str = ""          # ISO8601 UTC
    last_scanned: int = 0           # 上一轮发现的待分析条数
    last_analyzed: int = 0          # 上一轮真正跑完分析的条数
    analyzed_total: int = 0
    errors_total: int = 0

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "ticks": self.ticks,
                "running": self.running,
                "last_tick_at": self.last_tick_at,
                "last_scanned": self.last_scanned,
                "last_analyzed": self.last_analyzed,
                "analyzed_total": self.analyzed_total,
                "errors_total": self.errors_total,
            }


STAT = DispatcherStat()


def find_pending(db: Session, limit: int) -> list[ProblemRecord]:
    """ACTIVE 且从未有过分析记录 (SUCCESS/FAILED 都算有过) 的归档。

    按产生时间正序: 老问题优先, 不让新告警把队列挤后。
    """
    analyzed = select(AIAnalysisRecord.problem_record_id).distinct()
    rows = db.scalars(
        select(ProblemRecord)
        .where(ProblemRecord.status == "ACTIVE")
        .where(ProblemRecord.id.not_in(analyzed))
        .order_by(ProblemRecord.started_at.asc())
        .limit(limit)
    ).all()
    return list(rows)


def pending_count(db: Session) -> int:
    """当前待自动分析数 (GET /ai/auto 用, 与 find_pending 同口径不带 limit)。"""
    analyzed = select(AIAnalysisRecord.problem_record_id).distinct()
    return db.scalar(
        select(func.count())
        .select_from(ProblemRecord)
        .where(ProblemRecord.status == "ACTIVE")
        .where(ProblemRecord.id.not_in(analyzed))
    ) or 0


def run_tick(max_per_tick: int) -> tuple[int, int]:
    """跑一轮调度 (同步函数, 线程池执行)。返回 (发现数, 完成分析数)。

    成功/失败/异常都算"处理过": analyze_problem_with_tools 内部已把
    SUCCESS/FAILED 落库, 下一轮 find_pending 自然跳过, 不会重试风暴。
    """
    llm = get_default_llm_client()
    if not llm.configured():
        # 未配置: 本轮什么都不做, 也不推进计数 (状态端点仍诚实显示 configured=False)
        return 0, 0

    n_pending = 0
    analyzed = 0
    errors = 0
    with STAT.lock:
        STAT.running = True
    try:
        with SessionLocal() as db:
            pending = find_pending(db, limit=max_per_tick)
            n_pending = len(pending)
            for problem in pending:
                try:
                    record = analyze_problem_with_tools(
                        db, problem.id, trigger_source="AUTO"
                    )
                    analyzed += 1
                    # Phase 13: 自动分析成功 → 按操作目录生成"待人工审批"的
                    # 执行建议 (只建单据, 零执行); 建议失败不影响分析落库。
                    if record.status == "SUCCESS":
                        try:
                            req = suggest_for_problem(db, problem, record)
                            if req is not None:
                                logger.info(
                                    "已生成执行建议 (待审批): request=%s "
                                    "op=%s problem=%s",
                                    req.id, req.op_id, problem.id,
                                )
                        except Exception:
                            logger.exception(
                                "生成执行建议失败 (不影响分析): problem=%s",
                                problem.id,
                            )
                except LLMError as exc:
                    # FAILED 行已由 ai_agent 落库, 这里只计数与告警
                    logger.warning(
                        "自动分析失败 (已归档 FAILED): problem=%s err=%s",
                        problem.id, exc,
                    )
                    analyzed += 1  # 有 FAILED 记录, 下轮不再重排 → 算处理过
                except Exception:
                    logger.exception("自动分析异常: problem=%s", problem.id)
                    errors += 1
    finally:
        with STAT.lock:
            STAT.running = False
            STAT.ticks += 1
            STAT.last_tick_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            STAT.last_scanned = n_pending
            STAT.last_analyzed = analyzed
            STAT.analyzed_total += analyzed
            STAT.errors_total += errors
    # Phase 14: 轮次持久化入链 (还 P12 "STAT 只在内存" 的债) ——
    # 只记"干了活或出了错"的轮次, 空转轮不刷屏; db=None 自开独立会话。
    if analyzed > 0 or errors > 0:
        with STAT.lock:
            snap = {
                "ticks": STAT.ticks,
                "analyzed_total": STAT.analyzed_total,
                "errors_total": STAT.errors_total,
            }
        audit.record_event(
            event_type=audit.EV_DISPATCHER_TICK,
            actor="dispatcher", source="AUTO",
            object_type="dispatcher_tick", object_id=snap["ticks"],
            detail={
                "scanned": n_pending, "analyzed": analyzed, "errors": errors,
                "analyzed_total": snap["analyzed_total"],
                "errors_total": snap["errors_total"],
            },
        )
    return n_pending, analyzed


async def auto_analyze_loop() -> None:
    """后台周期调度 (main.lifespan 拉起)。任何异常只记日志, 下轮继续。"""
    settings = get_settings()
    interval = settings.ai_auto_interval
    logger.info(
        "AI 自动分析调度已启动 (interval=%ss, max_per_tick=%s)",
        interval,
        settings.ai_auto_max_per_tick,
    )
    while True:
        try:
            n_pending, n_analyzed = await asyncio.to_thread(
                run_tick, settings.ai_auto_max_per_tick
            )
            if n_pending:
                logger.info(
                    "自动分析: 本轮待办=%s 完成=%s", n_pending, n_analyzed
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("自动分析调度异常 (%ss 后重试)", interval)
            with STAT.lock:
                STAT.errors_total += 1
        await asyncio.sleep(interval)
