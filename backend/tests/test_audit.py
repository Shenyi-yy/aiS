"""Phase 14 单元测试: 审计哈希链 (建链/重放校验/篡改检测/只追加结构/写入点接线)。

隔离方式与 test_dispatcher 同款: 本模块独立临时库 + 私有 :memory: 引擎 +
dependency_overrides[get_db], 不污染共享测试库的空表断言。
"""

import os
import tempfile

_test_dir = tempfile.mkdtemp(prefix="zops_audit_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/audit.db"
os.environ["ZABBIX_PASSWORD"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_MODEL"] = ""

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.base import Base, get_db, init_db  # noqa: E402
from app.db.models import (  # noqa: E402
    AIAnalysisRecord,
    AuditEvent,
    ExecutionRecord,
    ProblemRecord,
)
from app.main import app  # noqa: E402
from app.services import (  # noqa: E402
    ai_agent,
    ai_dispatcher,
    audit,
    docker_exec,
    execution,
)

init_db()  # TestClient 不走 lifespan: 手工建表 (含 audit_events) 给路由用
client = TestClient(app)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=_engine)
_TestSession = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


@pytest.fixture(scope="module", autouse=True)
def _override_db():
    """覆盖 get_db 但必须还原: test_exec 在 import 期就设了覆盖,
    直接 pop 会把它的覆盖一并清掉 (跨模块串库 → 路由 404 的隐性炸弹)。"""

    def _override():
        with _TestSession() as db:
            yield db

    _sentinel = object()
    prev = app.dependency_overrides.get(get_db, _sentinel)
    app.dependency_overrides[get_db] = _override
    yield
    if prev is _sentinel:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = prev


@pytest.fixture(autouse=True)
def _clean():
    """审计表跨用例累积, 每个用例从干净链开始。"""
    with _TestSession() as s:
        for model in (AuditEvent, ExecutionRecord, AIAnalysisRecord, ProblemRecord):
            s.query(model).delete()
        s.commit()
    yield


def _mk_problem(s, eventid: str = "90001") -> ProblemRecord:
    p = ProblemRecord(
        zabbix_eventid=eventid, host="ubuntu-host",
        trigger_name="ZOPS审计测试: 容器 zops-drill 已停止", severity=4,
        status="ACTIVE",
        started_at=datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc),
        raw_payload="{}",
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


# ---------- 链本身 ----------

def test_chain_genesis_links_and_verify_ok() -> None:
    with _TestSession() as db:
        r1 = audit.record_event(db=db, event_type="t.a", actor="x", detail={"n": 1})
        r2 = audit.record_event(db=db, event_type="t.b", actor="x", detail={"n": 2})
        r3 = audit.record_event(db=db, event_type="t.c", actor="x", detail={"n": 3})
        assert r1 is not None and r2 is not None and r3 is not None
        assert r1.prev_hash == audit.GENESIS_HASH
        assert r2.prev_hash == r1.row_hash
        assert r3.prev_hash == r2.row_hash
        assert len(r1.row_hash) == 64
        out = audit.verify_chain(db)
        assert out["ok"] is True and out["checked"] == 3
        assert out["first_broken_id"] is None


def test_tamper_detail_is_detected_at_that_row() -> None:
    with _TestSession() as db:
        audit.record_event(db=db, event_type="t.a", detail={"orig": True})
        bad = audit.record_event(db=db, event_type="t.b", detail={"orig": True})
        audit.record_event(db=db, event_type="t.c", detail={"orig": True})
        db.execute(
            # 注意: JSON 里的冒号会被 text() 当绑定参数, 必须走 :d 传值
            text("UPDATE audit_events SET detail=:d WHERE id=:i"),
            {"d": '{"orig": false}', "i": bad.id},
        )
        db.commit()
        db.expire_all()  # 绕过身份映射: 让 verify_chain 从库里重读被篡改的行
        out = audit.verify_chain(db)
        assert out["ok"] is False
        assert out["first_broken_id"] == bad.id  # 直指被改那行, 不含糊
        assert "内容与签名不符" in out["reason"]


def test_delete_row_is_detected_at_next_row() -> None:
    """删一行再续链肉眼难辨, 但下一行 prev_hash 断节必被重放抓到。"""
    with _TestSession() as db:
        audit.record_event(db=db, event_type="t.a")
        audit.record_event(db=db, event_type="t.b")
        r3 = audit.record_event(db=db, event_type="t.c")
        db.query(AuditEvent).filter(AuditEvent.event_type == "t.b").delete()
        db.commit()
        out = audit.verify_chain(db)
        assert out["ok"] is False
        assert out["first_broken_id"] == r3.id
        assert "prev_hash 断链" in out["reason"]


def test_timestamp_naive_roundtrip_keeps_chain_valid() -> None:
    """SQLite 回读 naive datetime 是历史坑: 规范化口径必须让链依然成立。"""
    with _TestSession() as db:
        audit.record_event(db=db, event_type="t.ts", detail={"z": 1})
        db.expire_all()  # 强制从库重读, occurred_at 变 naive
        out = audit.verify_chain(db)
        assert out["ok"] is True


def test_record_event_never_raises_on_dead_session() -> None:
    """铁律: 审计写失败绝不拖垮业务 —— 坏会话返回 None 而不是炸出异常。

    注意: Session.close() 后仍可复用 (不算死), 所以用真会抛异常的对象。
    """

    class _BoomSession:
        def scalars(self, *a, **k):
            raise RuntimeError("boom: 库炸了")

        def rollback(self):
            pass

    assert audit.record_event(db=_BoomSession(), event_type="t.dead") is None


def test_audit_disabled_only_stops_writes(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "audit_enabled", False)
    with _TestSession() as db:
        assert audit.record_event(db=db, event_type="t.off") is None
        assert db.query(AuditEvent).count() == 0
    monkeypatch.setattr(settings, "audit_enabled", True)
    with _TestSession() as db:
        assert audit.record_event(db=db, event_type="t.on") is not None


# ---------- 写入点接线 ----------

def test_execution_lifecycle_writes_events() -> None:
    """create→approve 与 create→reject 路径都要留痕, actor=审批人。"""

    def fake_run(action, name):
        return {"ok": True, "state": "running", "http_status": 204}

    orig = docker_exec.run_container_action
    docker_exec.run_container_action = fake_run
    try:
        with _TestSession() as db:
            p = _mk_problem(db)
            req = execution.create_request(
                db, "restart_zops_drill", problem_record_id=p.id, source="MANUAL"
            )
            execution.approve_request(db, req.id, approver="杨仕毅", comment="演练")
            req2 = execution.create_request(
                db, "restart_zops_drill", problem_record_id=None, source="MANUAL"
            )
            execution.reject_request(db, req2.id, approver="张三", reason="不该做")
            evs = db.query(AuditEvent).order_by(AuditEvent.id).all()
            types = [e.event_type for e in evs]
            assert types == [
                audit.EV_EXEC_REQUEST_CREATED, audit.EV_EXEC_APPROVED,
                audit.EV_EXEC_REQUEST_CREATED, audit.EV_EXEC_REJECTED,
            ]
            approved = evs[1]
            assert approved.actor == "杨仕毅"
            assert "request_status" in approved.detail and "EXECUTED" in approved.detail
            assert approved.link_problem_id == p.id
            assert audit.verify_chain(db)["ok"] is True
    finally:
        docker_exec.run_container_action = orig


def test_agent_analysis_writes_audit_with_trigger_source(monkeypatch) -> None:
    """ai_agent 成功路径入链, detail 必须带 trigger_source (谁让模型跑的)。"""

    class _FakeLLM:
        model = "fake-audit-model"

        def configured(self) -> bool:
            return True

    def fake_run_agent(llm, messages, max_rounds):
        return ("结论OK", [{"tool": "zbx_event_get", "round": 1, "args": {},
                           "ok": True, "result_head": "", "tool_ms": 1}])

    monkeypatch.setattr(ai_agent, "get_default_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(ai_agent, "run_agent", fake_run_agent)
    with _TestSession() as db:
        p = _mk_problem(db, eventid="90002")
        rec = ai_agent.analyze_problem_with_tools(db, p.id, trigger_source="AUTO")
        assert rec.status == "SUCCESS"
        ev = db.query(AuditEvent).filter(
            AuditEvent.event_type == audit.EV_ANALYSIS_SUCCESS
        ).one()
        assert "trigger_source" in ev.detail and "AUTO" in ev.detail
        assert ev.object_id == str(rec.id)
        assert ev.link_problem_id == p.id


def test_dispatcher_tick_persistence_and_skip(monkeypatch) -> None:
    """P12 债的还法: 干了活的轮次入链; 空转轮不入链 (不刷屏)。"""
    captured: list[dict] = []

    def fake_record(**kw):
        captured.append(kw)
        return None

    monkeypatch.setattr(ai_dispatcher.audit, "record_event", fake_record)

    class _ConfiguredLLM:
        def configured(self) -> bool:
            return True

    def fake_analyze(db, problem_id, trigger_source="MANUAL"):
        rec = AIAnalysisRecord(
            problem_record_id=problem_id, host="h", trigger_name="t",
            model="m", status="SUCCESS", analysis="ok", trigger_source=trigger_source,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec

    monkeypatch.setattr(ai_dispatcher, "get_default_llm_client", lambda: _ConfiguredLLM())
    monkeypatch.setattr(ai_dispatcher, "SessionLocal", _TestSession)
    monkeypatch.setattr(ai_dispatcher, "analyze_problem_with_tools", fake_analyze)

    with _TestSession() as db:
        _mk_problem(db, eventid="90003")
    n, a = ai_dispatcher.run_tick(max_per_tick=3)
    assert (n, a) == (1, 1)
    tick_events = [c for c in captured
                   if c.get("event_type") == audit.EV_DISPATCHER_TICK]
    assert len(tick_events) == 1 and tick_events[0]["detail"]["analyzed"] == 1
    assert tick_events[0]["source"] == "AUTO"

    # 第二轮无待办: 空转轮不再入链
    captured.clear()
    ai_dispatcher.run_tick(max_per_tick=3)
    assert [c for c in captured
            if c.get("event_type") == audit.EV_DISPATCHER_TICK] == []


def test_record_event_own_session_path() -> None:
    """真走 record_event(db=None): 独立会话写本模块临时库, 必须不炸。"""
    row = audit.record_event(event_type="t.own_session", detail={"k": "v"})
    assert row is not None
    from app.db.base import SessionLocal as _AppSession

    with _AppSession() as sdb:
        assert sdb.query(AuditEvent).filter(
            AuditEvent.event_type == "t.own_session"
        ).count() == 1


# ---------- 路由与只追加结构 ----------

def test_audit_routes_registered_and_get_only() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    audit_paths = {p: v for p, v in paths.items() if p.startswith("/api/v1/audit")}
    assert set(audit_paths) == {
        "/api/v1/audit",
        "/api/v1/audit/verify",
        "/api/v1/audit/timeline/problem/{problem_id}",
    }
    # 只追加铁证: 三个路径下注册的 HTTP 方法全部只有 get
    for methods in (v.keys() for v in audit_paths.values()):
        assert set(methods) == {"get"}


def test_list_filter_and_verify_endpoints() -> None:
    with _TestSession() as db:
        p = _mk_problem(db, eventid="90005")
        audit.record_event(db=db, event_type=audit.EV_ANALYSIS_SUCCESS,
                           source="API", link_problem_id=p.id, detail={"a": 1})
        audit.record_event(db=db, event_type=audit.EV_DISPATCHER_TICK,
                           source="AUTO", detail={"b": 2})
    data = client.get("/api/v1/audit", params={"limit": 10}).json()["data"]
    assert data["total"] == 2
    assert data["items"][0]["row_hash"]  # 倒序: tick 在前
    only_api = client.get("/api/v1/audit", params={"source": "API"}).json()["data"]
    assert only_api["total"] == 1
    assert only_api["items"][0]["detail"] == {"a": 1}  # JSON 已解析回显
    bad = client.get("/api/v1/audit", params={"source": "WHATEVER"})
    assert bad.status_code == 422  # 口径收在枚举, 不接受随手糊弄
    v = client.get("/api/v1/audit/verify").json()["data"]
    assert v["ok"] is True and v["checked"] == 2


def test_timeline_assembles_full_closed_loop() -> None:
    with _TestSession() as db:
        p = _mk_problem(db, eventid="90006")
        rec = AIAnalysisRecord(
            problem_record_id=p.id, host=p.host, trigger_name=p.trigger_name,
            model="m", status="SUCCESS", analysis="定位: 容器停了",
            trigger_source="AUTO",
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        req = execution.create_request(
            db, "restart_zops_drill", problem_record_id=p.id, source="AUTO",
            analysis_id=rec.id,
        )
        audit.record_event(db=db, event_type=audit.EV_ANALYSIS_SUCCESS,
                           source="AUTO", link_problem_id=p.id,
                           object_type="ai_analysis_record", object_id=rec.id)
        rid, pid = req.id, p.id
    tl = client.get(f"/api/v1/audit/timeline/problem/{pid}").json()["data"]
    assert tl["problem"]["zabbix_eventid"] == "90006"
    assert [a["id"] for a in tl["analyses"]] == [rec.id]
    assert [e["id"] for e in tl["executions"]] == [rid]
    assert tl["executions"][0]["status"] == "PENDING_APPROVAL"  # 未被执行碰过
    assert len(tl["events"]) == 2  # create 入链 + 手工补的 success
    assert all(ev["row_hash"] for ev in tl["events"])
    miss = client.get("/api/v1/audit/timeline/problem/999999")
    assert miss.status_code == 404


def test_version_is_0160_and_schema_shape() -> None:
    info = client.get("/openapi.json").json()["info"]
    assert info["version"] == "0.16.0"
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert "ChainVerifyOut" in schema and "AuditEventOut" in schema
    assert "KnowledgeOut" in schema and "SearchOut" in schema


# ---------- 首轮 VM 验证后的两个加固点 (回归锁) ----------

def test_failed_audit_keeps_caller_pending_rows(monkeypatch) -> None:
    """审计写失败绝不连坐调用方会话里未提交的业务行。

    回归: 初版 except 路径直接 session.rollback() —— 调用方此刻若有
    未提交内容会被一起抹掉。现在审计行罩在 SAVEPOINT 里, 失败只撤自己;
    stage != savepoint 才允许 rollback (外层 commit 已被引擎中止的止损)。
    """
    def boom(prev_hash, payload_str):
        raise OperationalError("stmt", {}, Exception("generic boom"))

    monkeypatch.setattr(audit, "_sign", boom)
    with _TestSession() as db:
        pending = ProblemRecord(
            zabbix_eventid="90007", host="h",
            trigger_name="ZOPS审计测试: 失败不连坐", severity=1,
            status="ACTIVE",
            started_at=datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc),
            raw_payload="{}",
        )
        db.add(pending)  # 故意不提交: 审计失败必须让它活着
        assert audit.record_event(db=db, event_type="t.fail") is None
        db.commit()  # 调用方自己的节奏不受任何影响
        db.expire_all()
        assert db.query(ProblemRecord).filter_by(zabbix_eventid="90007").count() == 1
        assert db.query(AuditEvent).filter_by(event_type="t.fail").count() == 0


def test_lock_retry_then_success(monkeypatch) -> None:
    """第一次尝试遇 'database is locked' → 应用层重试成功, 事件恰好入链一次。"""
    calls = {"n": 0}
    real_sign = audit._sign

    def flaky(prev_hash, payload_str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("stmt", {}, Exception("database is locked"))
        return real_sign(prev_hash, payload_str)

    monkeypatch.setattr(audit, "_sign", flaky)
    with _TestSession() as db:
        row = audit.record_event(db=db, event_type="t.retry", detail={"n": 1})
        assert row is not None and calls["n"] == 2
        assert db.query(AuditEvent).filter_by(event_type="t.retry").count() == 1
        assert audit.verify_chain(db)["ok"] is True
