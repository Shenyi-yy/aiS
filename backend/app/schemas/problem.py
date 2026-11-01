"""/api/v1/problems/* 的出参模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProblemOut(BaseModel):
    """单条问题归档 (ORM → DTO)。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    zabbix_eventid: str
    host: str
    trigger_name: str
    severity: int
    status: str
    started_at: datetime
    recovered_at: datetime | None


class ProblemPage(BaseModel):
    """分页列表包装, 方便前端 Dashboard (Phase 6) 直接消费。"""

    total: int
    offset: int
    limit: int
    items: list[ProblemOut]


class SyncResult(BaseModel):
    """一次 Zabbix → problem_records 同步的统计。"""

    fetched: int
    inserted: int
    updated: int
    recovered_matched: int
