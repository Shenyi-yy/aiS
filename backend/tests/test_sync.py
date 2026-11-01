"""Phase 5 同步逻辑单元测试: apply_events 的 upsert/去重/恢复状态机。

这些 events 字典是**测试夹具** (模拟 Zabbix event.get 的返回形状),
只存在于测试内存库, 不会进入任何业务数据 —— 与"禁止 fake 监控数据"不冲突。

运行:
    cd backend
    pip install -r requirements-dev.txt
    python -m pytest tests -q
"""

import os
import tempfile

_test_dir = tempfile.mkdtemp(prefix="zops_test_sync_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/test.db"

from sqlalchemy import select  # noqa: E402

from app.db.base import Base, SessionLocal, engine  # noqa: E402
from app.db.models import ProblemRecord  # noqa: E402
from app.services.problem_sync import apply_events  # noqa: E402

Base.metadata.create_all(bind=engine)


def _event(eventid: str = "10001", clock: int = 1760000000) -> dict:
    return {
        "eventid": eventid,
        "name": "ZOPS: High CPU utilization on ubuntu-host",
        "clock": str(clock),
        "severity": "4",
        "hosts": [{"host": "10001", "name": "ubuntu-host"}],
    }


def test_apply_events_inserts_active_record() -> None:
    with SessionLocal() as db:
        result = apply_events(db, [_event()], {})
        assert result.fetched == 1 and result.inserted == 1 and result.updated == 0
        row = db.scalar(select(ProblemRecord))
        assert row is not None
        assert row.status == "ACTIVE"
        assert row.host == "ubuntu-host"
        assert row.recovered_at is None


def test_apply_events_is_idempotent() -> None:
    """同一 eventid 重复同步不产生第二条记录 (Phase 5 验证 [5] 的单元级保证)。"""
    with SessionLocal() as db:
        apply_events(db, [_event()], {})
    with SessionLocal() as db:
        result = apply_events(db, [_event()], {})
        assert result.inserted == 0 and result.updated == 0
        rows = db.scalars(select(ProblemRecord)).all()
        assert len(rows) == 1


def test_apply_events_marks_recovered_once() -> None:
    with SessionLocal() as db:
        apply_events(db, [_event()], {})
    with SessionLocal() as db:
        result = apply_events(db, [_event()], {"10001": 1760000600})
        assert result.updated == 1
        row = db.scalar(select(ProblemRecord))
        assert row.status == "RECOVERED"
        assert row.recovered_at is not None
