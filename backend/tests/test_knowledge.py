"""Phase 15 单元测试: BM25 检索 / 知识 CRUD 入链 / 闭环复盘诚实闸门 / 注入链路。

隔离方式与 test_audit 同款: 独立临时库 + 私有 :memory: 引擎 +
dependency_overrides[get_db] (还原前值, 不炸别的模块的覆盖)。
"""

import os
import tempfile
from datetime import datetime, timezone

_test_dir = tempfile.mkdtemp(prefix="zops_knowledge_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/kb.db"
os.environ["ZABBIX_PASSWORD"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_MODEL"] = ""

import json  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.base import Base, get_db, init_db  # noqa: E402
from app.db.models import (  # noqa: E402
    AIAnalysisRecord,
    AuditEvent,
    ExecutionRecord,
    KnowledgeEntry,
    ProblemRecord,
)
from app.main import app  # noqa: E402
from app.services import ai_agent, audit, knowledge  # noqa: E402
from app.services.knowledge import (  # noqa: E402
    KnowledgeNotFound,
    KnowledgeUnready,
)

init_db()  # TestClient 不走 lifespan: 手工建表 (含 knowledge_entries) 给路由用
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
    """覆盖 get_db 但必须还原他模块的覆盖 (口径同 test_audit)。"""

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


def _mk_problem(s, eventid: str = "95001") -> ProblemRecord:
    p = ProblemRecord(
        zabbix_eventid=eventid, host="ubuntu-host",
        trigger_name="ZOPS演练: 容器 zops-drill 已停止", severity=4,
        status="RECOVERED",
        started_at=datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc),
        raw_payload="{}",
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


# ---------- 分词与 BM25 ----------

def test_tokenize_ascii_and_cjk_bigrams() -> None:
    toks = knowledge.tokenize("Docker restart zops-drill 容器已停止")
    assert "docker" in toks and "zops-drill" in toks
    # 中文 bigram: "容器已停止" → 容器/器已/已停/停止
    assert "容器" in toks and "停止" in toks and "器已" in toks
    assert knowledge.tokenize("") == []


def test_retrieve_ranks_and_refuses_to_fabricate() -> None:
    with _TestSession() as db:
        knowledge.create_entry(
            db, title="zops-drill 容器停止处置手册", content="先 docker ps 确认, 再走审批重启",
            category="RUNBOOK", tags="docker,zops-drill",
        )
        knowledge.create_entry(
            db, title="Nginx 502 排查", content="上游超时与 worker 配置",
            category="DOC", tags="nginx",
        )
        hits = knowledge.retrieve(db, "zops-drill 容器 停止", top_k=3)
        assert len(hits) == 1  # Nginx 那条没资格硬凑进来
        assert hits[0][0].title.startswith("zops-drill")
        assert hits[0][1] > 0
        # 乱码/无关查询: 诚实零命中, 不返回"最不像但存在"的结果
        assert knowledge.retrieve(db, "量子隧穿效应", top_k=3) == []


# ---------- 写操作入审计链 (P14 口径延伸) ----------

def test_knowledge_writes_are_audited_and_chain_survives() -> None:
    with _TestSession() as db:
        row = knowledge.create_entry(
            db, title="磁盘水位清理 RUNBOOK", content="du -x 定位后人工确认再删",
            category="RUNBOOK", tags="disk",
        )
        knowledge.update_entry(db, row.id, tags="disk,logging")
        knowledge.delete_entry(db, row.id)
        # 只认本条目的事件 (同模块前面测试也造过条目, 不按全表断言)
        types = [
            e.event_type
            for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
            if e.event_type.startswith("knowledge.") and e.object_id == str(row.id)
        ]
        assert types == [
            audit.EV_KNOWLEDGE_CREATED,
            audit.EV_KNOWLEDGE_UPDATED,
            audit.EV_KNOWLEDGE_DELETED,
        ]
        assert audit.verify_chain(db)["ok"] is True
        assert db.get(KnowledgeEntry, row.id) is None


# ---------- 闭环复盘: 诚实闸门 ----------

def test_case_requires_real_problem_and_success_analysis() -> None:
    with _TestSession() as db:
        with pytest.raises(KnowledgeNotFound):
            knowledge.build_case_document(db, 999999)
        p = _mk_problem(db, "95002")
        with pytest.raises(KnowledgeUnready):
            knowledge.build_case_document(db, p.id)  # 有归档没分析 → 拒绝
        a = AIAnalysisRecord(
            problem_record_id=p.id, host=p.host, trigger_name=p.trigger_name,
            model="m", status="SUCCESS", analysis="## 现象摘要\n容器被人工停止",
            trigger_source="AUTO",
        )
        db.add(a)
        db.commit()
        db.refresh(a)
        e = ExecutionRecord(
            problem_record_id=p.id, analysis_id=a.id,
            op_id="restart_zops_drill", action="restart", target="zops-drill",
            status="EXECUTED", approver="杨仕毅", result="HTTP 204 running",
        )
        db.add(e)
        db.commit()
        audit.record_event(
            db=db, event_type=audit.EV_ANALYSIS_SUCCESS, actor="llm-agent",
            link_problem_id=p.id,
        )
        entry, created = knowledge.build_case_document(db, p.id)
        assert created is True and entry.category == "CASE"
        assert entry.source_type == "CLOSED_LOOP"
        assert entry.problem_record_id == p.id
        assert "容器被人工停止" in entry.content  # 分析正文进了复盘
        assert "杨仕毅" in entry.content  # 处置证据进了复盘
        again, created2 = knowledge.build_case_document(db, p.id)
        assert created2 is False and again.id == entry.id  # 幂等: 不重复语料


# ---------- 路由层 ----------

def test_knowledge_routes_crud_search_and_gates() -> None:
    r = client.post("/api/v1/knowledge", json={
        "title": "路由测试条目", "content": "zops-drill 路由验证正文",
        "category": "DOC", "tags": "pytest",
    })
    assert r.status_code == 200
    rid = r.json()["data"]["id"]
    # 复盘类别没有手工写入口 (schema 正则收口)
    assert client.post("/api/v1/knowledge", json={
        "title": "x2", "content": "y", "category": "CLOSED_LOOP",
    }).status_code == 422
    # /search 没被 /{entry_id} 吃掉 (路由顺序坑的回归闸)
    r = client.get("/api/v1/knowledge/search", params={"q": "zops-drill 路由"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["corpus_total"] >= 1 and data["count"] >= 1
    assert data["hits"][0]["entry"]["id"] == rid and data["hits"][0]["score"] > 0
    r = client.get("/api/v1/knowledge/search", params={"q": "阿巴阿巴乱码查询"})
    assert r.status_code == 200 and r.json()["data"]["count"] == 0
    assert client.get(f"/api/v1/knowledge/{rid}").status_code == 200
    assert client.get("/api/v1/knowledge/424242").status_code == 404
    r = client.put(f"/api/v1/knowledge/{rid}", json={"tags": "pytest,updated"})
    assert r.status_code == 200 and "updated" in r.json()["data"]["tags"]
    # 复盘闸门 HTTP 语义: 问题不存在 404
    assert client.post("/api/v1/knowledge/case/999999").status_code == 404
    assert client.delete(f"/api/v1/knowledge/{rid}").status_code == 200
    assert client.get(f"/api/v1/knowledge/{rid}").status_code == 404


# ---------- Agent 注入链路 (round=0) ----------

def test_rag_injection_visible_in_messages_and_trace(monkeypatch) -> None:
    class ScriptedLLM:
        model = "fake-kb-model"

        def __init__(self):
            self.payloads = [
                {"choices": [{"message": {"content": "结论", "tool_calls": None}}]}
            ]
            self.calls: list[list] = []

        def chat_raw(self, messages, tools=None, temperature=0.2, max_tokens=None):
            self.calls.append([dict(m) for m in messages])
            return self.payloads.pop(0)

    llm = ScriptedLLM()
    monkeypatch.setattr(ai_agent, "get_default_llm_client", lambda: llm)
    with _TestSession() as db:
        knowledge.create_entry(
            db, title="闭环复盘: ZOPS演练 容器 zops-drill 已停止",
            content="历史处置: 审批后 docker restart 恢复 running",
            category="CASE", tags="zops-drill",
        )
        p = _mk_problem(db, "95003")
        rec = ai_agent.analyze_problem_with_tools(db, p.id, trigger_source="AUTO")
        assert rec.status == "SUCCESS"
        trace = json.loads(rec.tool_trace)
        rag = trace[0]
        assert rag["round"] == 0 and rag["tool"] == "rag_retrieve"
        assert rag["hits"] >= 1 and "闭环复盘" in rag["result_head"]
        # 命中才注入: 第一轮 messages 里必须带"知识库检索参考" system 消息
        refs = [
            m["content"] for m in llm.calls[0]
            if m["role"] == "system" and "知识库检索参考" in m["content"]
        ]
        assert refs, "有命中却未注入参考"
        # 注入的参考必须是真实条目正文片段
        assert "审批后 docker restart" in refs[0]
        # 审计 detail 的 tools_used 含 rag_retrieve ("查没查知识库"可审)
        ev = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == audit.EV_ANALYSIS_SUCCESS)
            .order_by(AuditEvent.id.desc())
        ).first()
        assert "rag_retrieve" in ev.detail
