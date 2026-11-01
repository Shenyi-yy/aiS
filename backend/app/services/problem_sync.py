"""问题归档同步 (Phase 5): Zabbix API 真实事件 → problem_records 表。

数据流: event.get(PROBLEM) + event.get(RECOVERY) → 按 zabbix_eventid 幂等 upsert。
本模块不直接碰 Zabbix API —— 全部通过 services/zabbix_client 的语义方法。
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import ProblemRecord
from app.schemas.problem import SyncResult
from app.services.zabbix_client import ZabbixClient, get_default_client

logger = logging.getLogger("zops.services.problem_sync")

MAX_EVENTS_PER_RUN = 500


def _from_clock(clock: int) -> datetime:
    return datetime.fromtimestamp(clock, tz=timezone.utc)


def apply_events(
    db: Session,
    events: list[dict],
    recovery_clocks: dict[str, int],
) -> SyncResult:
    """把事件列表幂等写入 problem_records (单元测试与同步任务共用)。"""
    inserted = 0
    updated = 0
    for event in events:
        eventid = str(event["eventid"])
        host = (event.get("hosts") or [{}])[0].get("name", "unknown")
        new_status = "RECOVERED" if eventid in recovery_clocks else "ACTIVE"
        recovered_clock = recovery_clocks.get(eventid)

        row = db.scalar(
            select(ProblemRecord).where(ProblemRecord.zabbix_eventid == eventid)
        )
        if row is None:
            row = ProblemRecord(
                zabbix_eventid=eventid,
                host=host,
                trigger_name=str(event.get("name", "")),
                severity=int(event.get("severity") or 0),
                status=new_status,
                started_at=_from_clock(int(event["clock"])),
                recovered_at=(
                    _from_clock(int(recovered_clock)) if recovered_clock else None
                ),
                raw_payload=json.dumps(event, ensure_ascii=False),
            )
            db.add(row)
            inserted += 1
        else:
            changed = False
            if row.status != new_status:
                row.status = new_status
                changed = True
            if new_status == "RECOVERED" and row.recovered_at is None and recovered_clock:
                row.recovered_at = _from_clock(int(recovered_clock))
                changed = True
            if changed:
                updated += 1
    db.commit()
    return SyncResult(
        fetched=len(events),
        inserted=inserted,
        updated=updated,
        recovered_matched=len(recovery_clocks),
    )


def sync_recent_problems(
    client: ZabbixClient, db: Session, days: int
) -> SyncResult:
    """一次完整同步: 拉取近 N 天真实事件并归档。API 失败抛 ZabbixError。"""
    import time

    time_from = int(time.time()) - days * 86400
    events = client.fetch_problem_events(time_from, limit=MAX_EVENTS_PER_RUN)
    recoveries = client.resolve_recovery_clocks(events)
    result = apply_events(db, events, recoveries)
    logger.info(
        "Zabbix 问题同步完成: fetched=%s inserted=%s updated=%s recovered=%s (近 %s 天)",
        result.fetched,
        result.inserted,
        result.updated,
        result.recovered_matched,
        days,
    )
    return result


def sync_once(days: int | None = None) -> SyncResult:
    """给后台任务/路由用的便捷入口: 自管 Session。"""
    if days is None:
        from app.config import get_settings

        days = max(get_settings().zabbix_sync_days, 1)
    with SessionLocal() as db:
        return sync_recent_problems(get_default_client(), db, days)
