"""/api/v1/ai/* 的出参模型 (Phase 9 LLM 接入)。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class LLMConfigOut(BaseModel):
    """LLM 配置状态。

    注意: 只回显 Key 的长度, 永不回显 Key 内容 —— 大盘/验证脚本据此
    判断"环境变量是否注入成功" (与 Phase 5 判定 ZABBIX_PASSWORD 的手法一致)。
    """

    configured: bool
    base_url: str
    model: str
    api_key_len: int
    timeout_seconds: int


class CheckResult(BaseModel):
    """POST /ai/check: 一次真实最小往返的结果。"""

    ok: bool
    model: str
    latency_ms: int


class AnalysisOut(BaseModel):
    """单条 AI 分析归档 (ORM → DTO)。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    problem_record_id: int
    host: str
    trigger_name: str
    model: str
    status: str
    analysis: str
    error: str
    tool_trace: str
    trigger_source: str = "MANUAL"  # Phase 12: AUTO=后台调度 / MANUAL=手工
    prompt_tokens: int
    completion_tokens: int
    elapsed_ms: int
    created_at: datetime


class AnalysisPage(BaseModel):
    """分页列表包装, 与 ProblemPage 同款形状, 大盘可直接复用。"""

    total: int
    offset: int
    limit: int
    items: list[AnalysisOut]


class ToolInfo(BaseModel):
    """单个 Zabbix 工具声明 (Phase 10)。"""

    name: str
    description: str
    readonly: bool  # 恒为 True: 白名单里根本没有写操作, 这里是可核验的证据


class ToolsList(BaseModel):
    """GET /ai/tools: 工具白名单回显 + 循环上限。"""

    max_rounds: int
    tools: list[ToolInfo]


class AutoStatus(BaseModel):
    """GET /ai/auto: Phase 12 自动分析调度运行状态 (配置+实时计数)。"""

    enabled: bool
    running_now: bool
    interval_seconds: int
    max_per_tick: int
    max_rounds: int
    llm_configured: bool
    model: str
    pending: int              # 当前 ACTIVE 且无分析记录的归档数
    ticks: int                # 启动以来跑过的轮数
    last_tick_at: str         # 上一轮完成时间 (ISO8601 UTC, 空=还没跑过)
    last_scanned: int
    last_analyzed: int
    analyzed_total: int
    errors_total: int
