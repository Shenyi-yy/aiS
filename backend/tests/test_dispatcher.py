"""Phase 12 单元测试: 自动分析调度 (待办口径 / 单轮限量 / 不重复排队 / 状态端点)。

与 test_ai.py 同款隔离: 本文件用独立 :memory: 引擎, 不往共享测试库插
problem_records (test_api 的空表断言依赖它)。run_tick 里的 SessionLocal
用 monkeypatch 换成私有会话工厂。
"""

import os
import tempfile
from datetime import datetime, timezone

_test_dir = tempfile.mkdtemp(prefix="zops_disp_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/shared.db"
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_MODEL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.base import Base, init_db  # noqa: E402
from app.db.models import AIAnalysisRecord, ExecutionRecord, ProblemRecord  # noqa: E402
from app.main import app  # noqa: E402
from app.services import ai_dispatcher  # noqa: E402
from app.services.llm_client import LLMError  # noqa: E402

init_db()  # TestClient 不走 lifespan: 手工建表给路由测试用
client = TestClient(app)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=_engine)
_TestSession = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _mk_problem(db, eventid: str, status: str = "ACTIVE") -> ProblemRecord:
    row = ProblemRecord(
        zabbix_eventid=eventid,
        host="ubuntu-host",
        trigger_name=f"ZOPS测试: 事件 {eventid}",
        severity=4,
        status=status,
        started_at=datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc),
        raw_payload="{}",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _ConfiguredLLM:
    model = "fake-auto-model"

    def configured(self) -> bool:
        return True


def _reset_stat() -> None:
    s = ai_dispatcher.STAT
    with s.lock:
        s.ticks = 0
        s.running = False
        s.last_tick_at = ""
        s.last_scanned = 0
        s.last_analyzed = 0
        s.analyzed_total = 0
        s.errors_total = 0


def _clean_tables() -> None:
    """私有内存库跨用例累积, 每个 run_tick 测试从干净表开始。"""
    with _TestSession() as db:
        db.query(AIAnalysisRecord).delete()
        db.query(ProblemRecord).delete()
        db.query(ExecutionRecord).delete()
        db.commit()


def _mk_record(db, problem_id: int, status: str, source: str) -> AIAnalysisRecord:
    row = AIAnalysisRecord(
        problem_record_id=problem_id, host="ubuntu-host",
        trigger_name="x", model="m", status=status,
        analysis="ok" if status == "SUCCESS" else "",
        error="" if status == "SUCCESS" else "fake",
        trigger_source=source,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_find_pending_only_unanalyzed_active() -> None:
    _clean_tables()
    with _TestSession() as db:
        p_active = _mk_problem(db, "80001")
        p_rec = _mk_problem(db, "80002", status="RECOVERED")
        p_done = _mk_problem(db, "80003")
        _mk_record(db, p_done.id, "FAILED", "AUTO")  # FAILED 也算处理过
        got = [p.id for p in ai_dispatcher.find_pending(db, limit=10)]
        assert got == [p_active.id]  # ACTIVE 未分析 only; 正序
        assert ai_dispatcher.pending_count(db) == 1


def test_run_tick_analyzes_once_and_never_requeues(monkeypatch) -> None:
    _clean_tables()
    calls: list[tuple[int, str]] = []

    def fake_analyze(db, problem_id, trigger_source="MANUAL"):
        calls.append((problem_id, trigger_source))
        # P13 起 run_tick 会用返回的记录做目录建议, fake 必须返回记录本体
        return _mk_record(db, problem_id, "SUCCESS", trigger_source)

    monkeypatch.setattr(ai_dispatcher, "get_default_llm_client", lambda: _ConfiguredLLM())
    monkeypatch.setattr(ai_dispatcher, "SessionLocal", _TestSession)
    monkeypatch.setattr(ai_dispatcher, "analyze_problem_with_tools", fake_analyze)
    _reset_stat()

    with _TestSession() as db:
        p = _mk_problem(db, "80101")
    n, a = ai_dispatcher.run_tick(max_per_tick=3)
    assert (n, a) == (1, 1)
    assert calls == [(p.id, "AUTO")]  # 后台调度必须记 AUTO
    snap = ai_dispatcher.STAT.snapshot()
    assert snap["ticks"] == 1 and snap["analyzed_total"] == 1
    assert snap["last_tick_at"] and snap["running"] is False

    # 第二轮: 已有记录 → 不再重排 (防重复烧 token)
    n2, a2 = ai_dispatcher.run_tick(max_per_tick=3)
    assert (n2, a2) == (0, 0)
    assert len(calls) == 1


def test_run_tick_failed_row_still_counts_as_processed(monkeypatch) -> None:
    _clean_tables()
    def failing_analyze(db, problem_id, trigger_source="MANUAL"):
        _mk_record(db, problem_id, "FAILED", trigger_source)
        raise LLMError("fake 供应商炸了")

    monkeypatch.setattr(ai_dispatcher, "get_default_llm_client", lambda: _ConfiguredLLM())
    monkeypatch.setattr(ai_dispatcher, "SessionLocal", _TestSession)
    monkeypatch.setattr(ai_dispatcher, "analyze_problem_with_tools", failing_analyze)
    _reset_stat()

    with _TestSession() as db:
        _mk_problem(db, "80201")
    n, a = ai_dispatcher.run_tick(max_per_tick=3)
    assert (n, a) == (1, 1)  # FAILED 也算处理过, 不进入重试风暴
    n2, _ = ai_dispatcher.run_tick(max_per_tick=3)
    assert n2 == 0


def test_run_tick_skips_when_llm_unconfigured(monkeypatch) -> None:
    class _Unconfigured:
        def configured(self) -> bool:
            return False

    monkeypatch.setattr(ai_dispatcher, "get_default_llm_client", lambda: _Unconfigured())
    _reset_stat()
    assert ai_dispatcher.run_tick(max_per_tick=3) == (0, 0)
    assert ai_dispatcher.STAT.snapshot()["ticks"] == 0  # 不推进计数, 不装忙


def test_run_tick_limits_per_tick(monkeypatch) -> None:
    _clean_tables()
    calls: list[int] = []

    def fake_analyze(db, problem_id, trigger_source="MANUAL"):
        calls.append(problem_id)
        # P13 起 run_tick 依赖返回值做目录建议
        return _mk_record(db, problem_id, "SUCCESS", trigger_source)

    monkeypatch.setattr(ai_dispatcher, "get_default_llm_client", lambda: _ConfiguredLLM())
    monkeypatch.setattr(ai_dispatcher, "SessionLocal", _TestSession)
    monkeypatch.setattr(ai_dispatcher, "analyze_problem_with_tools", fake_analyze)
    _reset_stat()

    with _TestSession() as db:
        for i in range(5):
            _mk_problem(db, f"8030{i}")
    n, a = ai_dispatcher.run_tick(max_per_tick=2)
    assert (n, a) == (2, 2)  # 告警风暴: 单轮限量, 额度不被烧穿


def test_auto_route_and_openapi_shape() -> None:
    resp = client.get("/api/v1/ai/auto")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["enabled"] is True          # 默认开关
    assert data["interval_seconds"] >= 30
    assert data["max_rounds"] >= 1
    assert data["llm_configured"] is False  # 测试环境未配 Key: 诚实呈现
    assert isinstance(data["pending"], int)
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/ai/auto" in paths
    # AnalysisOut 必须暴露 trigger_source (P13/P14 要按它分流/审计)
    schema = paths["/api/v1/ai/analyses"]["get"]["responses"]["200"]
    assert schema  # 路由在; 字段由 AutoStatus/AnalysisOut 模型保证


def test_run_tick_suggests_pending_execution_for_drill(monkeypatch) -> None:
    """P13 接线: 演练类问题分析成功 → run_tick 生成待审批单据 (零执行)。

    这是"AI 建议止步于单据"的证据测试: 整条链路跑完后唯一的新记录是
    PENDING_APPROVAL 的 ExecutionRecord, docker_exec 根本不会被 import
    路径触发 (本测试环境也没有 socket)。
    """
    _clean_tables()

    def fake_analyze(db, problem_id, trigger_source="MANUAL"):
        return _mk_record(db, problem_id, "SUCCESS", trigger_source)

    monkeypatch.setattr(ai_dispatcher, "get_default_llm_client", lambda: _ConfiguredLLM())
    monkeypatch.setattr(ai_dispatcher, "SessionLocal", _TestSession)
    monkeypatch.setattr(ai_dispatcher, "analyze_problem_with_tools", fake_analyze)
    _reset_stat()

    with _TestSession() as db:
        p = ProblemRecord(
            zabbix_eventid="80401", host="ubuntu-host",
            trigger_name="ZOPS演练: 容器 zops-drill 已停止",
            severity=4, status="ACTIVE",
            started_at=datetime(2026, 10, 3, 9, 30, tzinfo=timezone.utc),
            raw_payload="{}",
        )
        db.add(p)
        db.commit()
        pid = p.id

    n, a = ai_dispatcher.run_tick(max_per_tick=3)
    assert (n, a) == (1, 1)
    with _TestSession() as db:
        reqs = db.query(ExecutionRecord).all()
        assert len(reqs) == 1
        r = reqs[0]
        assert r.source == "AUTO" and r.status == "PENDING_APPROVAL"
        assert r.op_id == "restart_zops_drill" and r.target == "zops-drill"
        assert r.problem_record_id == pid

    # 再跑一轮: 已分析不重排, 建议也不许重复生成 (人只会被问一次)
    n2, _ = ai_dispatcher.run_tick(max_per_tick=3)
    assert n2 == 0
    with _TestSession() as db:
        assert db.query(ExecutionRecord).count() == 1
