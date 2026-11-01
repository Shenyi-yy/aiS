"""Phase 13 单元测试: 执行审批状态机 + 白名单闸门 + 建议→批准→执行闭环。

隔离方式 (与 test_dispatcher 同款): 私有 :memory: 引擎 + 依赖覆盖,
路由测试不再碰共享测试库; 结尾恢复 get_db 覆盖, 不污染后续模块。

说明 1: 测试里 monkeypatch 的是 docker_exec.run_container_action (执行出口),
返回的"容器状态"只存在于内存库的单据里, 不进任何业务数据, 与
"禁止 fake 监控数据"不冲突; 真实执行由 VM 上 verify_phase13.sh 负责。
说明 2: 本测试环境没有 docker.sock, 若某条路径真的尝试执行会直接
ExecError (诚实失败), 所以"建议路径零执行"由测试通过本身证明。

运行:
    cd backend
    pip install -r requirements-dev.txt
    python -m pytest tests -q
"""

import os
import tempfile
from datetime import datetime, timezone

_test_dir = tempfile.mkdtemp(prefix="zops_test_exec_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/test.db"
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_MODEL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.base import Base, get_db, init_db  # noqa: E402
from app.db.models import AIAnalysisRecord, ExecutionRecord, ProblemRecord  # noqa: E402
from app.main import app  # noqa: E402
from app.services import docker_exec, exec_catalog, execution  # noqa: E402

init_db()
client = TestClient(app)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=_engine)
_TestSession = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _override_db():
    with _TestSession() as db:
        yield db


app.dependency_overrides[get_db] = _override_db


@pytest.fixture(scope="module", autouse=True)
def _restore_db_override():
    yield
    app.dependency_overrides.pop(get_db, None)


def _mk_problem(db, eventid: str, trigger: str = "ZOPS演练: 容器 zops-drill 已停止") -> ProblemRecord:
    row = ProblemRecord(
        zabbix_eventid=eventid, host="ubuntu-host", trigger_name=trigger,
        severity=4, status="ACTIVE",
        started_at=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc),
        raw_payload="{}",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class FakeEngine:
    """记录被调用的 (action, target), 返回可控的容器状态。"""

    def __init__(self, state: str = "running", fail: bool = False) -> None:
        self.state = state
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def run(self, action, name):
        self.calls.append((action, name))
        if self.fail:
            raise docker_exec.ExecError("假引擎: permission denied")
        return {"ok": True, "state": self.state, "http_status": 204}


def _mk_fake(monkeypatch, **kw) -> FakeEngine:
    fake = FakeEngine(**kw)
    monkeypatch.setattr(execution.docker_exec, "run_container_action", fake.run)
    return fake


# ---------- 白名单结构证据 ----------

def test_operations_route_shape_and_whitelist() -> None:
    resp = client.get("/api/v1/exec/operations")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert sorted(data["allowed_actions"]) == ["restart", "start", "stop"]
    assert data["operations"], "默认配置下目录应非空"
    for op in data["operations"]:
        assert op["action"] in data["allowed_actions"]
        assert op["target"] == "zops-drill"  # 核心容器绝不出现在这里


def test_config_route_is_honest() -> None:
    resp = client.get("/api/v1/exec/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["approval_required"] is True
    assert data["allow_targets"] == ["zops-drill"]
    # 本机(Windows 开发环境)没有 /var/run/docker.sock: 必须诚实报 False
    assert data["socket_exists"] is False


def test_target_not_in_allowlist_hides_operation(monkeypatch) -> None:
    """收紧 EXEC_ALLOW_TARGETS=空 → 目录整体不可见, 创建请求被 409 拒绝。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "exec_allow_targets", "", raising=False)
    assert exec_catalog.visible_operations() == []
    resp = client.post(
        "/api/v1/exec/requests", json={"op_id": "restart_zops_drill"}
    )
    assert resp.status_code == 409
    assert "白名单" in resp.json()["detail"]


# ---------- 状态机: 创建 → 批准 → 执行 ----------

def test_create_then_approve_executes_and_records_evidence(monkeypatch) -> None:
    fake = _mk_fake(monkeypatch, state="running")
    with _TestSession() as db:
        p = _mk_problem(db, "91001")
        pid = p.id
    resp = client.post(
        "/api/v1/exec/requests",
        json={"op_id": "restart_zops_drill", "problem_record_id": pid},
    )
    assert resp.status_code == 200
    req = resp.json()["data"]
    assert req["status"] == "PENDING_APPROVAL" and req["source"] == "MANUAL"
    assert req["approver"] == ""  # 创建阶段零审批零执行
    assert fake.calls == []       # 创建接口绝不碰引擎

    ok = client.post(
        f"/api/v1/exec/requests/{req['id']}/approve",
        json={"approver": "yangshiyi", "comment": "同意: 按 AI 建议重启"},
    )
    assert ok.status_code == 200
    done = ok.json()["data"]
    assert fake.calls == [("restart", "zops-drill")]
    assert done["status"] == "EXECUTED"
    assert done["approver"] == "yangshiyi" and done["decided_at"]
    assert "running" in done["result"] and done["error"] == ""

    # 终态不可逆: 再批准/再拒绝都必须 409 (防重复执行)
    assert client.post(
        f"/api/v1/exec/requests/{done['id']}/approve",
        json={"approver": "yangshiyi"},
    ).status_code == 409
    assert client.post(
        f"/api/v1/exec/requests/{done['id']}/reject",
        json={"approver": "yangshiyi", "reason": "晚了"},
    ).status_code == 409


def test_state_verification_failure_lands_failed(monkeypatch) -> None:
    """restart 受理了但回读状态不是 running → FAILED, 不"发了请求就当成功"。"""
    _mk_fake(monkeypatch, state="restarting")
    req = client.post(
        "/api/v1/exec/requests", json={"op_id": "restart_zops_drill"}
    ).json()["data"]
    resp = client.post(
        f"/api/v1/exec/requests/{req['id']}/approve",
        json={"approver": "yangshiyi"},
    )
    assert resp.status_code == 502
    assert "FAILED" in resp.json()["detail"]
    body = client.get(f"/api/v1/exec/requests/{req['id']}").json()["data"]
    assert body["status"] == "FAILED" and "restarting" in body["error"]


def test_engine_error_lands_failed_with_approver_kept(monkeypatch) -> None:
    fake = _mk_fake(monkeypatch, fail=True)
    req = client.post(
        "/api/v1/exec/requests", json={"op_id": "restart_zops_drill"}
    ).json()["data"]
    resp = client.post(
        f"/api/v1/exec/requests/{req['id']}/approve",
        json={"approver": "yangshiyi", "comment": "试试"},
    )
    assert resp.status_code == 502
    assert "permission denied" in resp.json()["detail"]
    body = client.get(f"/api/v1/exec/requests/{req['id']}").json()["data"]
    assert body["status"] == "FAILED"
    assert body["approver"] == "yangshiyi"  # 失败也要留完整证据链
    assert fake.calls == [("restart", "zops-drill")]


def test_socket_channel_physically_absent_raises_execerror() -> None:
    """物理拔通道 (本机无 docker.sock) 的真实行为: 折叠成 ExecError,
    而不是裸 OSError —— P13 首跑 500 的直接回归。"""
    with pytest.raises(docker_exec.ExecError) as exc:
        docker_exec.run_container_action("restart", "zops-drill")
    assert "docker.sock" in str(exc.value)


def test_unknown_transport_error_lands_failed_not_500(monkeypatch) -> None:
    """执行出口抛任何异常 (如 socket 权限 OSError) 都必须落 FAILED + 502,
    审批证据链不许被裸 500 吞掉。"""

    def boom(action, name):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(execution.docker_exec, "run_container_action", boom)
    req = client.post(
        "/api/v1/exec/requests", json={"op_id": "restart_zops_drill"}
    ).json()["data"]
    resp = client.post(
        f"/api/v1/exec/requests/{req['id']}/approve",
        json={"approver": "yangshiyi"},
    )
    assert resp.status_code == 502
    body = client.get(f"/api/v1/exec/requests/{req['id']}").json()["data"]
    assert body["status"] == "FAILED"
    assert "Permission denied" in body["error"]


def test_approve_when_channel_disabled_returns_503(monkeypatch) -> None:
    """EXEC_ENABLED=false: 批准被拒且单据留在 PENDING, 引擎零调用。"""
    fake = _mk_fake(monkeypatch)
    monkeypatch.setattr(get_settings(), "exec_enabled", False, raising=False)
    req = client.post(
        "/api/v1/exec/requests", json={"op_id": "restart_zops_drill"}
    ).json()["data"]
    resp = client.post(
        f"/api/v1/exec/requests/{req['id']}/approve",
        json={"approver": "yangshiyi"},
    )
    assert resp.status_code == 503
    assert fake.calls == []
    body = client.get(f"/api/v1/exec/requests/{req['id']}").json()["data"]
    assert body["status"] == "PENDING_APPROVAL"  # 不静默吞, 也不误标终态


def test_reject_is_terminal_and_zero_execution(monkeypatch) -> None:
    fake = _mk_fake(monkeypatch)
    req = client.post(
        "/api/v1/exec/requests", json={"op_id": "restart_zops_drill"}
    ).json()["data"]
    ok = client.post(
        f"/api/v1/exec/requests/{req['id']}/reject",
        json={"approver": "yangshiyi", "reason": "先查根因再动手"},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["status"] == "REJECTED"
    assert ok.json()["data"]["comment"] == "先查根因再动手"
    assert fake.calls == []
    # 被拒后再批准 → 409: 人的否决不可被绕过
    assert client.post(
        f"/api/v1/exec/requests/{req['id']}/approve",
        json={"approver": "yangshiyi"},
    ).status_code == 409


def test_not_found_paths() -> None:
    assert client.post(
        "/api/v1/exec/requests", json={"op_id": "rm_everything"}
    ).status_code == 404
    assert client.post(
        "/api/v1/exec/requests/99999/approve", json={"approver": "yangshiyi"}
    ).status_code == 404
    assert client.get("/api/v1/exec/requests/99999").status_code == 404


def test_suggest_dedup_and_rejected_respected() -> None:
    """服务层不变式: 同一 (问题, 操作) 只建议一次, 含已拒绝的也不再审。"""
    with _TestSession() as db:
        p = _mk_problem(db, "91101", trigger="ZOPS演练: 容器 zops-drill 已停止")
        rec = AIAnalysisRecord(
            problem_record_id=p.id, host="ubuntu-host",
            trigger_name=p.trigger_name, model="m", status="SUCCESS",
            analysis="ok", trigger_source="AUTO",
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)

        first = execution.suggest_for_problem(db, p, rec)
        assert first is not None and first.source == "AUTO"
        assert first.status == "PENDING_APPROVAL"
        # 再建议: 已有单据 (PENDING) → None
        assert execution.suggest_for_problem(db, p, rec) is None
        # 人否决后再建议: 仍 None (否决必须被尊重)
        execution.reject_request(db, first.id, "yangshiyi", "不准")
        assert execution.suggest_for_problem(db, p, rec) is None
        # 不匹配的触发器永远不会产生建议
        p2 = _mk_problem(db, "91102", trigger="CPU 使用率过高")
        rec2 = AIAnalysisRecord(
            problem_record_id=p2.id, host="ubuntu-host",
            trigger_name=p2.trigger_name, model="m", status="SUCCESS",
            analysis="ok", trigger_source="AUTO",
        )
        db.add(rec2)
        db.commit()
        assert execution.suggest_for_problem(db, p2, rec2) is None


def test_execution_list_pagination_shape() -> None:
    resp = client.get("/api/v1/exec/requests?limit=5")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] >= 0 and isinstance(data["items"], list)
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/exec/requests/{request_id}/approve" in paths
    # 关键结构证据: AI 工具接口里不存在任何执行入口
    tool_names = {t["name"] for t in client.get("/api/v1/ai/tools").json()["data"]["tools"]}
    assert not any(("exec" in n) or ("run" in n) or ("restart" in n) for n in tool_names)
    assert ExecutionRecord.__tablename__ == "execution_records"
