"""Phase 7: Docker 监控汇总服务。

数据全部实时来自 Zabbix API (经唯一出口 zabbix_client), 本模块只做聚合:
- item.get  search key_="docker."   → 监控项覆盖与收数健康度
- trigger.get search description    → 模板触发器是否已实例化

路由层不出现任何 Zabbix API 细节 (项目铁律)。
"""

import re

from app.schemas.docker import (
    DockerHostStats,
    DockerItemView,
    DockerSummary,
    DockerTriggerView,
)
from app.services.zabbix_client import get_default_client

# docker.container.get[<uuid>,<key>,<prefix>] 之类 item key 的第一段参数
_UUID_IN_KEY_RE = re.compile(r"docker\.[A-Za-z.]+\[([^,\]\s]+)")
# Phase 11 实证: 7.0 Docker agent2 模板的容器级项是下划线键
# docker.container_info.<field>["/<容器名>"] (type=18 依赖项) —— 上面的
# [A-Za-z.] 字符类不含下划线, 一个都匹配不到; db=6/ps=6 而 summary=4
# 的另一半根因就是这个。名称参数系单独用这条精确匹配 (参数须是 "/name")。
_NAME_PARAM_RE = re.compile(r'\["/([^"\[\]]+)"')


def _is_image_param(param: str, key: str) -> bool:
    """排除模板"镜像发现系"混进容器计数 (Phase 11 二轮实证).

    docker.image.created["sha256:<64hex>"] 这类键无下划线, 会穿过旧
    _UUID_IN_KEY_RE 的 [A-Za-z.] 字符类被当成容器 —— discovered 12 =
    6 真容器 + 6 镜像 SHA。镜像不是容器, 参数含 sha256:/键以
    docker.image. 开头的一律剔除。
    """
    return key.startswith("docker.image.") or "sha256:" in param


def _is_image_family_key(key: str) -> bool:
    """镜像发现系键 (Phase 13 三轮口径): 只看键前缀, 判定更保守。

    背景: P11 曾手工禁用 docker.image.created/size 两项顽固噪音, 但每次
    `--build` 后端镜像都会产出新 sha256 → LLD 重新发现 → 新生 2 个必然
    预处理失败的项。镜像系早已不进容器计数, 其收数健康度也不该卡"容器
    数据面闸门"(红线只留给容器系 + usage_inactive 那类真实项); 数字不
    藏: 单列 item_unsupported_image 如实回显。
    """
    return key.startswith("docker.image.")


def build_summary(item_limit: int = 1000, trigger_limit: int = 100) -> DockerSummary:
    """聚合 Zabbix 侧 Docker 监控的真实状态 (无缓存, 每次全量实查)。

    Phase 11: item_limit 默认 300→1000 —— 旧默认在 6 容器现场发生真实截断
    (启用项~310 + 模板原型 44 按名排序切尾, 容器数被少报成 4)。
    """
    client = get_default_client()
    raw_items = client.fetch_monitor_items(key_prefix="docker.", limit=item_limit)
    raw_triggers = client.fetch_docker_triggers(limit=trigger_limit)

    items: list[DockerItemView] = []
    stats: dict[str, DockerHostStats] = {}
    container_ids: set[str] = set()

    for it in raw_items:
        hosts = it.get("hosts") or [{}]
        host_name = str(hosts[0].get("host") or "?")
        key = str(it.get("key_", ""))
        # 7.0 模板主流是名称参数系; UUID 系保留兼容老键形
        m = _NAME_PARAM_RE.search(key) or _UUID_IN_KEY_RE.search(key)
        # 模板原型项参数是 {#宏} (7.0 item.get 默认把原型一起搜出来),
        # 不是真实容器, 计数必须跳过
        if m and "{#" not in m.group(1) and not _is_image_param(m.group(1), key):
            container_ids.add(f"{host_name}:{m.group(1)}")
        view = DockerItemView(
            itemid=int(it["itemid"]),
            name=str(it.get("name", "")),
            key_=key,
            host=host_name,
            lastvalue=str(it.get("lastvalue", "") or ""),
            lastclock=int(it.get("lastclock", 0) or 0),
            state=int(it.get("state", 0) or 0),
            error=str(it.get("error", "") or ""),
        )
        items.append(view)
        s = stats.setdefault(
            host_name, DockerHostStats(host=host_name, item_count=0,
                                       unsupported_count=0, error_count=0)
        )
        s.item_count += 1
        if view.state == 1:
            s.unsupported_count += 1
        if view.error:
            s.error_count += 1

    triggers = [
        DockerTriggerView(
            triggerid=int(t["triggerid"]),
            description=str(t.get("description", "")),
            status=str(t.get("status", "0")),
            priority=int(t.get("priority", 0) or 0),
            host=str(((t.get("hosts") or [{}])[0]).get("host") or "?"),
        )
        for t in raw_triggers
    ]

    return DockerSummary(
        api_connected=True,
        item_total=len(items),
        # 闸门口径 = 容器系 (含主机级 docker.* 真实项) unsupported;
        # 镜像系模板噪音单列, 不隐身也不卡闸门 (见 _is_image_family_key)
        item_unsupported=sum(
            1 for i in items if i.state == 1 and not _is_image_family_key(i.key_)
        ),
        item_unsupported_image=sum(
            1 for i in items if i.state == 1 and _is_image_family_key(i.key_)
        ),
        item_with_error=sum(1 for i in items if i.error),
        discovered_container_count=len(container_ids),
        trigger_total=len(triggers),
        per_host=sorted(stats.values(), key=lambda s: s.host),
        items=items[:60],  # 响应瘦身: 明细最多带 60 条, 计数仍是全量
        triggers=triggers,
    )
