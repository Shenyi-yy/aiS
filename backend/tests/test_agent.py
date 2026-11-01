"""Phase 10 单元测试: tool-calling 循环 / 只读白名单 / 轮数护栏 / 轨迹落库。

FakeLLM 的脚本化 tool_calls 只存在于内存测试, 不进业务数据 (口径同 P9)。
真实模型工具往返由 VM 上 verify_phase10.sh 负责。

运行:
    cd backend
    pip install -r requirements-dev.txt
    python -m pytest tests -q
"""

import json
import os
import tempfile
from datetime import datetime, timezone

_test_dir = tempfile.mkdtemp(prefix="zops_test_agent_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/test.db"
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
from app.services import ai_agent  # noqa: E402
from app.services.llm_client import LLMError  # noqa: E402
from app.services.zabbix_tools import execute_tool, list_tool_names  # noqa: E402
from app.services.host_tools import execute_tool as execute_host_tool  # noqa: E402
from app.services.host_tools import list_tool_names as list_host_names  # noqa: E402
from app.services.ai_agent import ALL_TOOL_SCHEMAS, dispatch_tool  # noqa: E402

init_db()
client = TestClient(app)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=_engine)
_TestSession = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _mk_problem(db, eventid: str = "70001") -> ProblemRecord:
    row = ProblemRecord(
        zabbix_eventid=eventid,
        host="ubuntu-host",
        trigger_name="ZOPS演练: 容器 zops-drill 已停止",
        severity=4,
        status="RECOVERED",
        started_at=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc),
        raw_payload='{"eventid": "70001"}',
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _tool_call_payload(call_id: str, name: str, args: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ]
    }


def _content_payload(text: str) -> dict:
    return {"choices": [{"message": {"content": text, "tool_calls": None}}]}


class ScriptedLLM:
    """按脚本依次返回 payload; 记录每次收到的 messages。"""

    model = "fake-agent-model"

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls: list[list] = []

    def chat_raw(self, messages, tools=None, temperature=0.2, max_tokens=None):
        self.calls.append([dict(m) for m in messages])
        if tools is not None:
            # 生产契约: 每次问模型都必须带上只读工具白名单
            assert all(t["type"] == "function" for t in tools)
        if not self.payloads:
            raise AssertionError("模型被调用次数超出脚本 (轮数护栏失效?)")
        return self.payloads.pop(0)


def test_run_agent_two_rounds_collects_trace() -> None:
    llm = ScriptedLLM(
        [
            _tool_call_payload("c1", "zbx_get_event_detail", {"eventid": "70001"}),
            _content_payload("## 现象摘要\n结论正文"),
        ]
    )
    messages = [{"role": "user", "content": "工单"}]
    content, trace = ai_agent.run_agent(llm, messages, max_rounds=6)
    assert content == "## 现象摘要\n结论正文"
    assert len(trace) == 1
    step = trace[0]
    assert step["tool"] == "zbx_get_event_detail"
    assert step["args"] == {"eventid": "70001"}
    # 测试环境无 Zabbix 凭据: 工具失败被消化成观察 (ok=False), 不崩循环
    assert step["ok"] is False and "Zabbix" in step["result_head"]
    # 第二轮请求里必须已有 assistant(tool_calls) + tool(观察) 回填
    second = llm.calls[1]
    roles = [m["role"] for m in second]
    assert roles == ["user", "assistant", "tool"]
    assert second[1]["tool_calls"]  # assistant 原样带回
    assert json.loads(second[2]["content"])["error"]  # tool 观察是 JSON


def test_run_agent_round_guard_raises_with_trace() -> None:
    # 模型永远要调工具 → 护栏必须在 max_rounds 处切断
    llm = ScriptedLLM(
        [_tool_call_payload(f"c{i}", "zbx_get_active_triggers", {}) for i in range(3)]
    )
    with pytest.raises(LLMError) as excinfo:
        ai_agent.run_agent(llm, [{"role": "user", "content": "x"}], max_rounds=3)
    assert "最大工具轮数" in str(excinfo.value)
    assert len(getattr(excinfo.value, "trace")) == 3  # 已发生的轨迹带出去


def test_execute_tool_whitelist_rejects_write_like_names() -> None:
    for bad in ("zbx_drop_host", "item.create", "trigger.delete", "system.run"):
        obs = execute_tool(bad, {})
        assert "error" in obs and "白名单" in obs["error"]
    assert list_tool_names() == [
        "zbx_get_active_triggers",
        "zbx_get_event_detail",
        "zbx_get_host_items",
        "zbx_list_problem_events",
    ]


def test_execute_tool_param_errors_are_observations() -> None:
    assert "error" in execute_tool("zbx_get_event_detail", {})  # 缺 eventid
    assert "error" in execute_tool("zbx_get_host_items", {"host": ""})  # 缺 host


def test_agent_analysis_persists_success_with_trace(monkeypatch) -> None:
    llm = ScriptedLLM(
        [
            _tool_call_payload("c1", "zbx_list_problem_events", {"days": 7}),
            _content_payload("## 现象摘要\n(带工具的分析)"),
        ]
    )
    monkeypatch.setattr(ai_agent, "get_default_llm_client", lambda: llm)
    with _TestSession() as db:
        problem = _mk_problem(db)
        record = ai_agent.analyze_problem_with_tools(db, problem.id)
        assert record.status == "SUCCESS"
        assert "带工具" in record.analysis
        trace = json.loads(record.tool_trace)
        # P15: round=0 恒有 rag_retrieve 证据条目 (空知识库 → hits=0 不注入)
        assert trace[0]["round"] == 0 and trace[0]["tool"] == "rag_retrieve"
        assert trace[0]["hits"] == 0 and trace[0]["ok"] is True
        assert trace[1]["tool"] == "zbx_list_problem_events"


def test_agent_analysis_llm_error_records_failed_with_trace(monkeypatch) -> None:
    llm = ScriptedLLM(
        [
            _tool_call_payload("c1", "zbx_get_host_items", {"host": "ubuntu-host"}),
        ]
    )
    monkeypatch.setattr(ai_agent, "get_default_llm_client", lambda: llm)
    with _TestSession() as db:
        problem = _mk_problem(db, eventid="70002")
        # 补满 6 个 tool_call payload (= 默认 LLM_AGENT_MAX_ROUNDS):
        # 模型永远要调工具 → 触发轮数护栏 → LLMError → FAILED 行带全轨迹
        llm.payloads.append(_tool_call_payload("c2", "zbx_get_event_detail", {"eventid": "1"}))
        for _ in range(4):
            llm.payloads.append(_tool_call_payload("cx", "zbx_get_event_detail", {"eventid": "1"}))
        with pytest.raises(LLMError):
            ai_agent.analyze_problem_with_tools(db, problem.id)
        row = (
            db.query(AIAnalysisRecord)
            .filter_by(status="FAILED", problem_record_id=problem.id)
            .one()
        )
        assert "最大工具轮数" in row.error
        # FAILED 也留全轨迹: 1 条 round=0 rag_retrieve + 6 次模型工具调用
        assert len(json.loads(row.tool_trace)) == 7


def test_tools_endpoint_shape() -> None:
    resp = client.get("/api/v1/ai/tools")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["max_rounds"] == 6
    names = [t["name"] for t in data["tools"]]
    # P15: 三组只读工具合并成一张白名单 (4 zbx + 4 docker/host + 1 kb = 9)
    assert len(names) == 9
    assert all(n.startswith(("zbx_", "docker_", "host_", "kb_")) for n in names)
    assert all(t["readonly"] is True for t in data["tools"])
    # 白名单外没有任何写操作 (双重保险)
    assert not any(k in " ".join(names) for k in ("create", "delete", "update"))
    assert len(ALL_TOOL_SCHEMAS) == 9


def test_dispatch_routes_to_both_registries() -> None:
    # zbx 组仍由 zabbix_tools 处理 (无凭据 → 观察式错误)
    obs = dispatch_tool("zbx_get_event_detail", {"eventid": "1"})
    assert "error" in obs and "Zabbix" in obs["error"]
    # docker 组由 host_tools 处理
    obs = dispatch_tool("docker_list_containers", {"host": "ubuntu-host"})
    assert "error" in obs  # 测试环境无 Zabbix 凭据
    # 缺参路径在触网前就拦下
    obs = dispatch_tool("docker_container_detail", {"host": "ubuntu-host"})
    assert "container" in obs["error"]
    # P15: kb 组在闸内且缺参即拦 (只读检索, 不触网)
    obs = dispatch_tool("kb_search", {})
    assert "query" in obs["error"]
    obs = dispatch_tool("kb_search", {"query": "zops-drill 容器"})
    assert "error" not in obs and obs["count"] >= 0  # 空库诚实返回 count=0
    # 三组之外的名字一律拒绝 —— 合并白名单不放松闸门
    for bad in ("system.run", "docker.stop", "host_reboot", "kb_add", "kb_delete", ""):
        assert "白名单" in dispatch_tool(bad, {})["error"]


def test_host_tools_rejects_bad_container_names() -> None:
    for evil in ('zops"; drop', "a]b", 'x"y'):
        obs = execute_host_tool("docker_container_detail",
                                {"host": "h", "container": evil})
        assert "error" in obs, evil
    assert list_host_names() == [
        "docker_container_detail",
        "docker_get_summary",
        "docker_list_containers",
        "host_get_linux_metrics",
    ]


def test_agent_can_call_host_group_tool_and_traces_it(monkeypatch) -> None:
    llm = ScriptedLLM(
        [
            _tool_call_payload("c1", "docker_get_summary", {}),
            _content_payload("## 现象摘要\n容器侧结论"),
        ]
    )
    monkeypatch.setattr(ai_agent, "get_default_llm_client", lambda: llm)
    with _TestSession() as db:
        problem = _mk_problem(db, eventid="70003")
        record = ai_agent.analyze_problem_with_tools(db, problem.id)
        assert record.status == "SUCCESS"
        trace = json.loads(record.tool_trace)
        assert trace[0]["tool"] == "rag_retrieve"  # round=0 证据注入记录
        assert trace[1]["tool"] == "docker_get_summary"
