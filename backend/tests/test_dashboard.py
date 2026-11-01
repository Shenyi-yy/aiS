"""Phase 6 Dashboard 测试: 静态大盘挂载 + 路由不被吞。

不依赖 Zabbix / 网络, 纯本地可跑:
    cd backend
    python -m pytest tests -q
"""

import os
import tempfile

# 必须在导入 app 之前设置 (Settings 用 lru_cache, 导入即固化配置)
_test_dir = tempfile.mkdtemp(prefix="zops_dash_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/dash.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_root_serves_dashboard_html() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "ZOPS 监控大盘" in resp.text  # index.html 标题, 证明挂的是大盘而非 404


def test_health_not_shadowed_by_static_mount() -> None:
    """StaticFiles 挂在 '/' 且注册在路由之后, /health 必须仍是 JSON 存活检查。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "zops-backend"


def test_api_v1_routes_still_registered() -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for p in ("/api/v1/hosts", "/api/v1/problems", "/api/v1/system/info"):
        assert p in paths, f"{p} 未注册, 可能被静态挂载抢走或 router 漏注册"


def test_dashboard_html_consumes_only_real_endpoints() -> None:
    """大盘禁止假数据: JS 里出现的数据源必须是真实接口路径。"""
    resp = client.get("/")
    text = resp.text
    for endpoint in (
        "/api/v1/problems",
        "/api/v1/hosts",
        "/api/v1/system/info",
        "/api/v1/problems/sync",
    ):
        assert endpoint in text, f"大盘未引用 {endpoint}"
