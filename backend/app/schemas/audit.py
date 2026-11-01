"""Phase 14: 审计日志出参模型。

证据导向设计:
- 列表/时间线原样带出 prev_hash/row_hash —— 要让人能自行核对才叫防篡改;
- detail 存储是规范化 JSON 字符串, 出参解析成对象给人读 (解析失败原样
  退回 {"raw": ...}, 校验以字符串为准不受影响)。
"""

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class AuditEventOut(BaseModel):
    """单条审计事件 (ORM → DTO), 哈希链字段全带。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    actor: str
    source: str
    event_type: str
    object_type: str
    object_id: str
    link_problem_id: int | None
    detail: Any = {}
    prev_hash: str
    row_hash: str

    @field_validator("detail", mode="before")
    @classmethod
    def _parse_detail(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return json.loads(v) if v else {}
            except ValueError:
                return {"raw": v}
        return v


class AuditPage(BaseModel):
    """GET /api/v1/audit 分页结果。"""

    total: int
    offset: int
    limit: int
    items: list[AuditEventOut]


class ChainVerifyOut(BaseModel):
    """GET /api/v1/audit/verify 重放校验结论 (结构化, 不布尔糊弄)。"""

    ok: bool
    checked: int
    first_broken_id: int | None
    reason: str


class TimelineOut(BaseModel):
    """GET /api/v1/audit/timeline/problem/{id} 全链证据组装。

    主线闭环每一环都在: 归档问题 → 分析记录(含证据快照/轨迹/来源) →
    执行单据(建议/审批/结果三段) → 审计事件(哈希链)。缺环就是真没发生,
    接口不补假数据。
    """

    problem: dict[str, Any] | None
    analyses: list[dict[str, Any]]
    executions: list[dict[str, Any]]
    events: list[AuditEventOut]
