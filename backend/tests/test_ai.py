"""Phase 9 单元测试: LLM 未配置兜底 + 分析证据链 + FAILED 诚实落库。

说明 1: 测试里的 FakeLLM 返回文本只存在于内存库, 不进任何业务数据,
与"禁止 fake 监控数据"不冲突 (Phase 5 测试同理); 真实大模型连通性
由 VM 上的 verify_phase9.sh 负责。
说明 2: 本文件用独立的 :memory: 引擎 (不落进共享测试库), 避免
插入的 problem_records 影响其他测试模块对空表的断言。

运行:
    cd backend
    pip install -r requirements-dev.txt
    python -m pytest tests -q
"""

import os
import tempfile
from datetime import datetime, timezone

_test_dir = tempfile.mkdtemp(prefix="zops_test_ai_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/test.db"
# 强制清空 LLM 配置: 本机若存在 .env 也不能影响"未配置"分支断言
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_MODEL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.base import Base, init_db  # noqa: E402
from app.db.models import AIAnalysisRecord, ProblemRecord  # noqa: E402
from app.main import app  # noqa: E402
from app.services import ai_analyzer  # noqa: E402
from app.services.llm_client import LLMClient, LLMError  # noqa: E402

# TestClient 不走 lifespan, 手工建表保证路由测试能看到 problem_records / ai_analysis_records
init_db()
client = TestClient(app)

# ---------- 独立内存库 (不碰共享测试库) ----------
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=_engine)
_TestSession = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _mk_problem(db, eventid: str = "90001") -> ProblemRecord:
    row = ProblemRecord(
        zabbix_eventid=eventid,
        host="ubuntu-host",
        trigger_name="ZOPS演练: 容器 zops-drill 已停止",
        severity=4,
        status="RECOVERED",
        started_at=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc),
        recovered_at=datetime(2026, 10, 3, 8, 2, tzinfo=timezone.utc),
        raw_payload='{"eventid": "90001", "name": "drill"}',
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class FakeLLM:
    """测试替身: 记录收到的 messages, 可选抛错路径。"""

    model = "fake-test-model"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen_messages: list = []

    def chat(self, messages):
        self.seen_messages = messages
        if self.fail:
            raise LLMError("假供应商错误: 余额不足")
        return "## 现象摘要\n(测试分析)", {"prompt_tokens": 10, "completion_tokens": 5}


def test_llm_not_configured_config_reports_and_check_502() -> None:
    resp = client.get("/api/v1/ai/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["configured"] is False
    assert data["api_key_len"] == 0  # 只回显长度, 结构上不可能泄漏 Key
    bad = client.post("/api/v1/ai/check")
    assert bad.status_code == 502
    assert "未配置" in bad.json()["detail"]


def test_unconfigured_client_chat_raises_not_fake_ok() -> None:
    llm = LLMClient(base_url="", api_key="", model="")
    assert llm.configured() is False
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "hi"}])


def test_analyze_success_persists_with_evidence(monkeypatch) -> None:
    fake = FakeLLM()
    monkeypatch.setattr(ai_analyzer, "get_default_llm_client", lambda: fake)
    with _TestSession() as db:
        problem = _mk_problem(db)
        record = ai_analyzer.analyze_problem(db, problem.id)
        assert record.status == "SUCCESS"
        assert record.model == "fake-test-model"
        assert "现象摘要" in record.analysis
        assert record.prompt_tokens == 10 and record.completion_tokens == 5
        # 证据链真实: 喂给模型的 JSON 里必须有归档行 + Zabbix 对质字段
        snap = record.input_snapshot
        assert "ZOPS演练: 容器 zops-drill 已停止" in snap
        assert "zabbix_event" in snap  # 测试环境无 Zabbix, 值为 unavailable
        # system prompt 的硬约束在
        assert fake.seen_messages[0]["role"] == "system"
        assert "不得虚构" in fake.seen_messages[0]["content"]


def test_analyze_llm_failure_records_failed_row(monkeypatch) -> None:
    fake = FakeLLM(fail=True)
    monkeypatch.setattr(ai_analyzer, "get_default_llm_client", lambda: fake)
    with _TestSession() as db:
        problem = _mk_problem(db, eventid="90002")
        with pytest.raises(LLMError):
            ai_analyzer.analyze_problem(db, problem.id)
        row = db.query(AIAnalysisRecord).filter_by(status="FAILED").one()
        assert "余额不足" in row.error
        assert row.analysis == ""
        # 失败记录同样带证据快照: 复盘时知道当时喂了什么
        assert problem.trigger_name in row.input_snapshot


def test_analyze_unknown_problem_returns_404() -> None:
    resp = client.post("/api/v1/ai/analyze/999999")
    assert resp.status_code == 404


def test_analyses_list_shape() -> None:
    resp = client.get("/api/v1/ai/analyses")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0  # 共享测试库里没人插过分析 (内存库互不串)
    assert isinstance(data["items"], list)
