"""Phase 7 Docker 监控测试: 路由注册 + 无凭据降级 + 大盘引用新接口。

不需要 Zabbix 在线: 测试环境故意不给 ZABBIX_PASSWORD,
验证 /docker/summary 走 502 降级而不是假数据。
"""

import os
import tempfile

# 必须在导入 app 之前设置 (Settings 用 lru_cache)
_test_dir = tempfile.mkdtemp(prefix="zops_docker_")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir}/docker.db"
os.environ["ZABBIX_PASSWORD"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_docker_summary_route_registered() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/docker/summary" in paths


def test_docker_summary_502_without_credentials() -> None:
    """凭据缺失必须 502 + 明确报错, 绝不返回编造的 summary。"""
    resp = client.get("/api/v1/docker/summary")
    assert resp.status_code == 502
    assert "ZABBIX_PASSWORD" in resp.json()["detail"]


def test_dashboard_has_docker_panel() -> None:
    html = client.get("/").text
    assert "/api/v1/docker/summary" in html
    assert "Docker 容器监控" in html


def test_uuid_regex_extracts_container_ids() -> None:
    """聚合逻辑核心: 从 item key 第一段参数提取容器标识 (去重计数)。"""
    from app.services.docker_monitor import _UUID_IN_KEY_RE

    keys = [
        'docker.container.get[abc123def456,state,running]',
        'docker.container.get[abc123def456,memory,usage]',
        'docker.container.stats[789xyz000,cpu] ',
        "docker.version",
    ]
    found = {m.group(1) for k in keys if (m := _UUID_IN_KEY_RE.search(k))}
    assert found == {"abc123def456", "789xyz000"}


def test_summary_default_limit_no_container_truncation(monkeypatch) -> None:
    """Phase 11 回归: 旧默认 limit=300 在 6 容器现场把容器数少报成 4。

    伪造 360 项 (6 容器 × 60 项, 数量对齐真实现场"启用项+模板原型>300"),
    断言: ①默认调用带下来的 limit ≥ 1000 (口径别再退回截断值);
    ②discovered_container_count == 6 —— 排序尾部的容器不许被截丢。
    """
    from app.services import docker_monitor

    recorded: dict[str, object] = {}

    class _FakeClient:
        def fetch_monitor_items(self, key_prefix="docker.", limit=300):
            recorded["limit"] = limit
            out = []
            for c in range(6):
                for i in range(60):
                    out.append({
                        "itemid": 1000 + c * 60 + i,
                        "name": f"container c{c} metric {i}",
                        "key_": f'docker.container_info.m{i}["/c{c}"]',
                        "hosts": [{"host": "ubuntu-host", "name": "ubuntu-host"}],
                        "lastvalue": "1",
                        "lastclock": 1791000000,
                        "state": 0,
                        "error": "",
                    })
            # 模板原型项 (参数是 {#宏}) 混在里面也不许被算成容器
            out.append({
                "itemid": 9999,
                "name": "proto",
                "key_": 'docker.container_info.created["/{#CONTAINER.NAME}"]',
                "hosts": [{"host": "Docker by Zabbix agent 2", "name": "x"}],
                "lastvalue": "", "lastclock": 0, "state": 0, "error": "",
            })
            # 镜像发现系 (docker.image.*["sha256:...", 无下划线键) ——
            # Phase 11 二轮实证: 旧 UUID 正则会把 6 个镜像 SHA 数成容器
            for img in range(6):
                out.append({
                    "itemid": 20000 + img,
                    "name": f"image {img} created",
                    "key_": f'docker.image.created["sha256:{"ab" * 32}{img}"]',
                    "hosts": [{"host": "ubuntu-host", "name": "ubuntu-host"}],
                    "lastvalue": "", "lastclock": 0, "state": 0, "error": "",
                })
            return out

        def fetch_docker_triggers(self, limit=100):
            return []

    monkeypatch.setattr(docker_monitor, "get_default_client", lambda: _FakeClient())
    summary = docker_monitor.build_summary()
    assert recorded["limit"] >= 1000
    assert summary.item_total == 367
    assert summary.discovered_container_count == 6


def test_unsupported_split_container_vs_image_family(monkeypatch) -> None:
    """Phase 13 三轮口径: 镜像系 unsupported 单列, 不卡容器闸门也不隐身。

    现场复现: --build 后 LLD 为新镜像 sha 新生 created/size 两个预处理
    失败项 (state=1) —— item_unsupported 必须仍只数容器系那 1 个真问题项。
    """
    from app.services import docker_monitor

    def _item(iid: int, key: str, state: int) -> dict:
        return {
            "itemid": iid, "name": f"item {iid}", "key_": key,
            "hosts": [{"host": "ubuntu-host", "name": "ubuntu-host"}],
            "lastvalue": "", "lastclock": 0, "state": state, "error": "",
        }

    class _FakeClient:
        def fetch_monitor_items(self, key_prefix="docker.", limit=1000):
            return [
                _item(1, 'docker.container_info.m["/c1"]', 1),          # 容器系真问题
                _item(2, 'docker.image.created["sha256:aa"]', 1),       # 模板噪音
                _item(3, 'docker.image.size["sha256:aa"]', 1),          # 模板噪音
                _item(4, 'docker.container_info.cpu["/c1"]', 0),        # 正常
            ]

        def fetch_docker_triggers(self, limit=100):
            return []

    monkeypatch.setattr(docker_monitor, "get_default_client", lambda: _FakeClient())
    s = docker_monitor.build_summary()
    assert s.item_unsupported == 1            # 闸门只认容器系
    assert s.item_unsupported_image == 2      # 镜像系如实单列, 不藏
    assert s.item_total == 4
    assert s.discovered_container_count == 1


def test_name_param_regex_matches_underscore_keys() -> None:
    """Phase 11 实证: 7.0 模板容器级键是下划线系, 旧 _UUID_IN_KEY_RE 匹配不到。"""
    from app.services.docker_monitor import _NAME_PARAM_RE, _UUID_IN_KEY_RE

    real_keys = [
        'docker.container_info.state.running["/zops-drill"]',
        'docker.container_info.created["/zops-backend",full]',
    ]
    assert all(_UUID_IN_KEY_RE.search(k) is None for k in real_keys)  # 旧正则全军覆没
    got = {_NAME_PARAM_RE.search(k).group(1) for k in real_keys}
    assert got == {"zops-drill", "zops-backend"}
