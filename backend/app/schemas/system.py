"""/api/v1/system/* 的出参模型。"""

from pydantic import BaseModel


class SystemInfo(BaseModel):
    """系统自检信息 —— db_connected 来自真实 SELECT 1, 不是写死的 True。"""

    service: str
    env: str
    version: str
    db_dialect: str
    db_connected: bool
    zabbix_web_url: str
