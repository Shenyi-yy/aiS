"""/api/v1/zabbix/* 的出参模型。"""

from pydantic import BaseModel


class ZabbixHealth(BaseModel):
    """Zabbix API 连通自检 —— api_version 来自真实 apiinfo.version 调用。"""

    connected: bool
    api_version: str
