"""/api/v1/exec/* 的出入参模型 (Phase 13 人工确认 + 安全执行)。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ExecConfigOut(BaseModel):
    """执行通道配置回显: 全部真实读取, 供验证脚本判断注入是否生效。"""

    enabled: bool
    auto_suggest: bool
    docker_socket: str
    socket_exists: bool  # 容器里没挂 socket 时诚实报 False (拒绝执行而非假成功)
    allow_targets: list[str]
    timeout_seconds: int
    approval_required: bool  # 恒为 True: 结构上没有免审批路径, 可核验
    pending_requests: int


class OperationOut(BaseModel):
    """单条白名单操作 (无自由参数: 只有 action + target)。"""

    op_id: str
    name: str
    action: str
    target: str
    description: str
    risk: str


class OperationsList(BaseModel):
    """GET /exec/operations: 当前环境真正可执行的操作目录。"""

    allowed_actions: list[str]  # 固定动作集合 (证据: 只有这三个)
    operations: list[OperationOut]


class ExecutionCreateIn(BaseModel):
    """POST /exec/requests: 人工创建审批单 (仍停在待审批)。"""

    op_id: str = Field(min_length=1, max_length=64)
    problem_record_id: int | None = Field(default=None, ge=1)
    comment: str = Field(default="", max_length=2000)


class ApproveIn(BaseModel):
    """批准必须留名: approver 非空是审计链路 (P14) 的地基。"""

    approver: str = Field(min_length=2, max_length=64)
    comment: str = Field(default="", max_length=2000)


class RejectIn(BaseModel):
    approver: str = Field(min_length=2, max_length=64)
    reason: str = Field(default="", max_length=2000)


class ExecutionOut(BaseModel):
    """审批单 DTO (ORM → 出参), 建议/审批/执行三段证据全带。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    problem_record_id: int | None
    analysis_id: int | None
    op_id: str
    action: str
    target: str
    host: str
    source: str
    status: str
    proposal: str
    approver: str
    comment: str
    result: str
    error: str
    elapsed_ms: int
    decided_at: datetime | None
    created_at: datetime


class ExecutionPage(BaseModel):
    """分页包装, 与 ProblemPage / AnalysisPage 同款形状。"""

    total: int
    offset: int
    limit: int
    items: list[ExecutionOut]
