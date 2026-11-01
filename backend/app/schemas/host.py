"""/api/v1/hosts 出参模型 (Phase 6)。"""

from pydantic import BaseModel


class HostInfo(BaseModel):
    """Zabbix 受监控主机的展示视图。"""

    hostid: int
    technical_name: str
    name: str
    monitored: bool
