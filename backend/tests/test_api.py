"""Phase 4 API 测试: 分层骨架 (system/info, problems, 建表)。

本地运行 (Windows Git Bash 或容器内):
    cd backend
    pip install -r requirements-dev.txt
    python -m pytest tests -q

说明: 用临时 SQLite 文件, 不污染开发库。
"""

import os
import tempfile

# 必须在导入 app 之前设置: Settings 用 lru_cache, 导入即固化配置
_test_dir = tempfile.mkdtemp(prefix="zops_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_system_info_reports_real_db_connection() -> None:
    resp = client.get("/api/v1/system/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["db_connected"] is True  # 真实 SELECT 1 通过
    assert data["db_dialect"] == "sqlite"
    assert data["service"] == "zops-backend"


def test_problems_returns_empty_page_not_fake_data() -> None:
    resp = client.get("/api/v1/problems")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Phase 5 前该表就应该是空的 —— 出现数据反而是造假信号
    assert data["total"] == 0
    assert data["items"] == []


def test_problems_rejects_bad_status_filter() -> None:
    resp = client.get("/api/v1/problems", params={"status": "BOGUS"})
    assert resp.status_code == 422


def test_problem_records_table_created() -> None:
    from sqlalchemy import inspect

    from app.db.base import engine

    assert "problem_records" in inspect(engine).get_table_names()
