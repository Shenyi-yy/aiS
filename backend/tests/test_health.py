"""Phase 1 基础测试: /health 接口。

运行 (本地或容器内):
    pytest backend/tests -q
依赖 httpx (TestClient 需要), 已加入 requirements.txt。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "zops-backend"
