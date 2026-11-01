"""Phase 14: 审计日志服务 —— 只追加 + sha256 哈希链的跨模块证据流。

链式结构 (每行签名覆盖"前一行签名 + 本行内容"):

    GENESIS(64个0) → row1.row_hash → row2.prev_hash → row2.row_hash → ...

- 改任何一行的任意字段 → 该行重放签名对不上;
- 删行/换序/伪造 prev_hash → 前后链节对不上;
- GET /api/v1/audit/verify 全量重放, 报告第一个断链行 id —— 篡改或许
  能骗过单行肉眼, 骗不过重放校验。

诚实与可用性约束 (设计时定的三条铁律):
1. record_event **永不抛异常**: 审计写失败只记日志返回 None, 绝不把
   业务链路 (分析/审批/执行) 拖下水; 写入包在 SAVEPOINT 里, 失败也
   **绝不连坐调用方会话** (首轮 VM 验证后的加固点)。
2. **没有 update/delete 函数**: 服务层结构上不存在改删路径, 路由层只有 GET。
3. 时间戳参与哈希前规范化为 "%Y-%m-%dT%H:%M:%SZ" (UTC 整秒):
   SQLite 回读的 datetime 丢失 tzinfo, 直接 isoformat 会与写入时哈希的
   字符串不一致 → 校验必炸。规范化是唯一稳定的哈希口径。
"""

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import AuditEvent

logger = logging.getLogger("zops.services.audit")

# 链起点: 首行的 prev_hash。固定字面量, 校验与建链两端口径一致。
GENESIS_HASH = "0" * 64

# 追加锁: 建链"读最后一行 → 写新行"必须串行, 防并发分叉。
# SQLite 提交本身串行, 但这把锁让意图明确且对并发写入安全。
_append_lock = threading.Lock()

# 锁冲突重试: busy_timeout(30s) 之外再给应用层 3 次机会 (SAVEPOINT 级
# 升级冲突可能立刻报 SQLITE_BUSY, 不等满 30s)。
_LOCK_TRIES = 3

# —— 事件类型常量 (verify 脚本与大盘按这些字面量对质, 不许散写字符串) ——
EV_SYSTEM_STARTUP = "system.startup"
EV_ANALYSIS_SUCCESS = "analysis.success"
EV_ANALYSIS_FAILED = "analysis.failed"
EV_DISPATCHER_TICK = "dispatcher.tick"
EV_EXEC_REQUEST_CREATED = "execution.request_created"
EV_EXEC_APPROVED = "execution.approved"
EV_EXEC_REJECTED = "execution.rejected"
EV_KNOWLEDGE_CREATED = "knowledge.created"
EV_KNOWLEDGE_UPDATED = "knowledge.updated"
EV_KNOWLEDGE_DELETED = "knowledge.deleted"


def _ts_str(dt: datetime) -> str:
    """时间戳哈希口径: UTC 整秒字符串 (naive 视为 UTC)。

    写入时 datetime 带 tzinfo, SQLite 回读后为 naive —— 两者经本函数
    归一化后字符串完全一致, 哈希才可重放。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(detail: Any) -> str:
    """detail JSON 规范化: 键排序 + 紧凑分隔符, 消除序列化顺序自由度。"""
    return json.dumps(
        detail if detail is not None else {},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )


def _row_payload_str(row: AuditEvent) -> str:
    """参与哈希的本行内容 (不含 row_hash 自身; prev_hash 含内)。"""
    return json.dumps(
        {
            "id": row.id,
            "occurred_at": _ts_str(row.occurred_at),
            "actor": row.actor,
            "source": row.source,
            "event_type": row.event_type,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "link_problem_id": row.link_problem_id,
            # detail 入库前已 _canonical 化, 哈希直接取存储原文:
            # 若被改成非法 JSON 或任意内容, 重算签名必然对不上 (即篡改证据);
            # 若在哈希前做 json.loads 再序列化, 非法 JSON 会当场炸掉校验。
            "detail": row.detail,
            "prev_hash": row.prev_hash,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _sign(prev_hash: str, payload_str: str) -> str:
    return hashlib.sha256((prev_hash + payload_str).encode("utf-8")).hexdigest()


def _is_locked(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def record_event(
    *,
    event_type: str,
    actor: str = "",
    source: str = "SYSTEM",
    object_type: str = "",
    object_id: str | int = "",
    link_problem_id: int | None = None,
    detail: Any = None,
    db: Session | None = None,
) -> AuditEvent | None:
    """追加一条审计事件并接链; 永不抛异常 (见模块铁律 1)。

    db=None 时自开独立会话 (调度器/启动钩子用); 业务服务里传自己的 db,
    与业务落库同库同会话节奏 —— 但审计提交失败不回滚业务, 各自 commit。

    Phase 14 首轮 VM 验证后加固的三点 (对应排查结论):
    - 审计行写入包在 SAVEPOINT (begin_nested) 里: 失败只回滚审计行自己,
      绝不 rollback 调用方会话 —— 调用方的事务归调用方支配;
    - "database is locked" 应用层重试 _LOCK_TRIES 次 (每次重读链尾,
      prev_hash 跟着最新链走, 不会插错位置);
    - 只有外层 commit 真的失败 (事务已被引擎中止) 才 rollback 恢复会话
      可用性 —— 此时调用方的未提交内容本就随事务一起没了, 回滚只是止损。
    """
    if not get_settings().audit_enabled:
        return None
    own_session = db is None
    session = db if db is not None else SessionLocal()
    stage = "savepoint"
    try:
        for attempt in range(_LOCK_TRIES):
            try:
                with _append_lock:
                    with session.begin_nested():  # SAVEPOINT: 审计行的隔离罩
                        last = session.scalars(
                            select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)
                        ).first()
                        prev_hash = last.row_hash if last is not None else GENESIS_HASH
                        row = AuditEvent(
                            occurred_at=datetime.now(timezone.utc),
                            actor=actor[:64],
                            source=source[:16],
                            event_type=event_type[:48],
                            object_type=object_type[:32],
                            object_id=str(object_id)[:32],
                            link_problem_id=link_problem_id,
                            detail=_canonical(detail),
                            prev_hash=prev_hash,
                        )
                        session.add(row)
                        session.flush()  # 先拿到自增 id —— id 参与哈希, 改 id 也算篡改
                        row.row_hash = _sign(row.prev_hash, _row_payload_str(row))
                    stage = "commit"
                    session.commit()
                stage = "refresh"
                session.refresh(row)
                return row
            except OperationalError as exc:
                if not _is_locked(exc) or attempt == _LOCK_TRIES - 1:
                    raise
                logger.warning(
                    "审计写入遇锁, %sms 后重试 (%s/%s): type=%s",
                    int(0.5 * (attempt + 1) * 1000), attempt + 1, _LOCK_TRIES,
                    event_type,
                )
                time.sleep(0.5 * (attempt + 1))
    except Exception:
        logger.exception("审计事件写入失败 (业务不受影响): type=%s", event_type)
        if stage != "savepoint":
            # 外层事务已被引擎中止: rollback 只为把会话恢复到可用状态。
            # savepoint 阶段的异常则**绝不**碰调用方事务 (铁律: 不连坐)。
            try:
                session.rollback()
            except Exception:
                pass
        return None
    finally:
        if own_session:
            session.close()


def verify_chain(db: Session) -> dict[str, Any]:
    """全量重放校验哈希链, 返回结构化结论 (绝不布尔糊弄)。

    逐行核对两件事: ①prev_hash 是否等于前一行 row_hash (链节);
    ②按存储内容重算签名是否等于 row_hash (内容)。
    返回 first_broken_id 指向**第一个出问题的那一行**。
    """
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.asc())).all()
    expected_prev = GENESIS_HASH
    for row in rows:
        if row.prev_hash != expected_prev:
            return {
                "ok": False,
                "checked": len(rows),
                "first_broken_id": row.id,
                "reason": (
                    f"行 {row.id} prev_hash 断链: 指向 {row.prev_hash[:16]}..., "
                    f"应为 {expected_prev[:16]}... (删行/换序/改链节)"
                ),
            }
        recomputed = _sign(row.prev_hash, _row_payload_str(row))
        if recomputed != row.row_hash:
            return {
                "ok": False,
                "checked": len(rows),
                "first_broken_id": row.id,
                "reason": (
                    f"行 {row.id} 内容与签名不符 (row_hash={row.row_hash[:16]}..., "
                    f"重算={recomputed[:16]}...) —— 字段被改而签名未同步"
                ),
            }
        expected_prev = row.row_hash
    return {
        "ok": True,
        "checked": len(rows),
        "first_broken_id": None,
        "reason": "" if rows else "链为空 (尚无审计事件)",
    }
