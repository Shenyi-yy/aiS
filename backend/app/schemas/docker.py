"""Phase 7: Docker 监控汇总出参模型。"""

from pydantic import BaseModel


class DockerItemView(BaseModel):
    """单个 Zabbix docker.* 监控项的实时视图 (来自 item.get, 非本地缓存)。"""

    itemid: int
    name: str
    key_: str
    host: str
    lastvalue: str
    lastclock: int
    state: int
    error: str


class DockerHostStats(BaseModel):
    """按主机聚合的 Docker 监控覆盖情况 (统计真实 item 得出, 不猜容器状态)。"""

    host: str
    item_count: int
    unsupported_count: int
    error_count: int


class DockerTriggerView(BaseModel):
    """已实例化的 Docker 相关触发器 (来自 trigger.get)。"""

    triggerid: int
    description: str
    status: str
    priority: int
    host: str


class DockerSummary(BaseModel):
    """GET /api/v1/docker/summary 的聚合结果。

    诚实设计: discovered_container_count 数的是 item key 里出现的
    容器 UUID 去重个数 (docker.container.get[<uuid>,...] 第一段参数),
    即 Zabbix LLD 真正发现并建了监控项的容器数, 不额外造假。
    """

    api_connected: bool
    item_total: int
    item_unsupported: int
    # Phase 13 口径拆分: 镜像发现系 (docker.image.*["sha256:..."]) 的
    # unsupported 单列在这里 —— 每次 --build 都会因新镜像 sha 新生 2 个
    # 模板预处理失败项, 属已知环境噪音; 数字如实回显, 不并进容器闸门。
    item_unsupported_image: int = 0
    item_with_error: int
    discovered_container_count: int
    trigger_total: int
    per_host: list[DockerHostStats]
    items: list[DockerItemView]
    triggers: list[DockerTriggerView]
