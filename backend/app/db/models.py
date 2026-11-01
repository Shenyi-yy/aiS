"""ZOPS 业务模型 (Phase 4 建表, Phase 5 接 Zabbix API 后写入真实数据)

诚实原则: problem_records 现在就是空表 —— 它等的是 Phase 5 从 Zabbix
problem.get 拉回来的真实告警, 不做任何假数据填充。
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    """统一时区感知的 UTC 时间, 便于后续与 Zabbix event.clock (UNIX 秒) 对齐。"""
    return datetime.now(timezone.utc)


class ProblemRecord(Base):
    """Zabbix 问题归档: 以 zabbix_eventid 唯一去重, ACTIVE/RECOVERED 状态机。

    后续阶段 (Phase 12 AI 分析 / Phase 14 审计) 以 id 外键挂接分析与执行记录。
    """

    __tablename__ = "problem_records"
    __table_args__ = (
        UniqueConstraint("zabbix_eventid", name="uq_problem_eventid"),
        Index("ix_problem_status_severity", "status", "severity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zabbix_eventid: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Zabbix events.eventid"
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False, comment="主机名")
    trigger_name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="触发器 description"
    )
    severity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Zabbix trigger priority 0-5"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", comment="ACTIVE / RECOVERED"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="问题产生时间"
    )
    recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="恢复时间, 未恢复为 NULL"
    )
    raw_payload: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="Zabbix 原始事件 JSON, 排障用"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class AIAnalysisRecord(Base):
    """Phase 9: LLM 故障分析归档 —— 每次真实调用一行, 失败也落库。

    诚实原则:
    - status=SUCCESS 仅代表模型真的返回了内容; 调用失败同样写一行 FAILED,
      错误原因进 error 字段。分析列表里不存在"假装成功"的记录。
    - input_snapshot 保存当时真正喂给模型的证据 JSON, 事后可复盘
      "AI 是根据什么说的这话" (Phase 14 审计链路的雏形)。
    """

    __tablename__ = "ai_analysis_records"
    __table_args__ = (
        Index("ix_analysis_problem_record", "problem_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    problem_record_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("problem_records.id"),
        nullable=False,
        comment="外键 problem_records.id, 分析对象必是真实归档问题",
    )
    host: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="冗余主机名, 归档可读"
    )
    trigger_name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="冗余触发器名"
    )
    model: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="分析所用模型名"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SUCCESS", comment="SUCCESS / FAILED"
    )
    analysis: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="模型回复正文 (FAILED 时为空)"
    )
    error: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因 (SUCCESS 时为空)"
    )
    input_snapshot: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="喂给模型的证据 JSON"
    )
    tool_trace: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="Phase 10: 工具调用链 JSON (非 agent 分析为空串)",
    )
    trigger_source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="MANUAL",
        comment="Phase 12: AUTO=后台调度自动分析 / MANUAL=接口手工触发",
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="供应商 usage 回传的输入 token"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="输出 token"
    )
    elapsed_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="模型往返耗时 (毫秒)"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class AuditEvent(Base):
    """Phase 14: 审计事件 —— 哈希链串起的只追加证据流 (全平台关键动作留痕)。

    诚实与防篡改设计:
    - row_hash = sha256(prev_hash + 本行规范化 JSON); prev_hash 指向前一行
      row_hash, 首行指向 GENESIS (64 个 0)。改任何一行的任何字段、删行、
      换序都会让重放校验断裂 —— GET /audit/verify 报告第一个断链 id。
    - 只有 INSERT 路径: 服务层不暴露修改/删除函数, 路由层只有 GET。
    - link_problem_id 挂 problem_records.id, 时间线端点据此组装
      "告警→分析→建议→审批→执行" 全链证据。
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_event_time", "event_type", "occurred_at"),
        Index("ix_audit_problem", "link_problem_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        comment="事件发生时间 (哈希取整秒, 规范化后参与签名)",
    )
    actor: Mapped[str] = mapped_column(
        String(64), nullable=False, default="",
        comment="行为人/组件: 审批人名 / llm-agent / dispatcher / system",
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="API",
        comment="触发面: API=接口触发 / AUTO=后台自动 / SYSTEM=进程自身",
    )
    event_type: Mapped[str] = mapped_column(
        String(48), nullable=False,
        comment="system.startup / analysis.success / dispatcher.tick / "
                "execution.request_created / execution.approved / ...",
    )
    object_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="关联对象类型"
    )
    object_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="关联对象主键 (字符串化)"
    )
    link_problem_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("problem_records.id"), nullable=True,
        comment="关联归档问题 (独立事件为 NULL)",
    )
    detail: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
        comment="事件细节 JSON (规范化排序后序列化, 参与哈希)",
    )
    prev_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="前一行 row_hash, 首行为 GENESIS"
    )
    row_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="本行内容签名 sha256 hex"
    )


class ExecutionRecord(Base):
    """Phase 13: 执行请求审批单 —— 建议(AI/目录) 与 执行(人批准) 分离的凭证。

    状态机: PENDING_APPROVAL → EXECUTED / FAILED / REJECTED (终态不可逆)。
    - source=AUTO: 后台在自动分析成功后按目录匹配生成的建议;
      source=MANUAL: 人主动创建的请求。两者都必须停在 PENDING_APPROVAL,
      只有 POST .../approve 会真正碰 Docker 引擎 —— AI 没有任何执行路径。
    - 诚实原则: EXECUTED 以执行后回读的容器状态为准 (restart→running),
      达不到期望状态落 FAILED 并带引擎/状态原文, 绝不"发了请求就当成功"。
    """

    __tablename__ = "execution_records"
    __table_args__ = (
        Index("ix_execution_status", "status"),
        Index("ix_execution_problem", "problem_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    problem_record_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("problem_records.id"), nullable=True,
        comment="关联归档问题; 纯手工请求可为空",
    )
    analysis_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_analysis_records.id"), nullable=True,
        comment="建议来源的分析记录 (AUTO 建议必有)",
    )
    op_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="exec_catalog 目录操作 id"
    )
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="冗余目录动作: restart/start/stop"
    )
    target: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="冗余目标容器名 (白名单内)"
    )
    host: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="冗余归档主机名, 可读性"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="MANUAL",
        comment="AUTO=后台按目录自动生成 / MANUAL=人工创建",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING_APPROVAL",
        comment="PENDING_APPROVAL / EXECUTED / FAILED / REJECTED",
    )
    proposal: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="建议理由快照 (含触发器/分析定位)"
    )
    approver: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="审批人 (批准或拒绝都记)"
    )
    comment: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="审批意见 / 拒绝原因"
    )
    result: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="执行结果回读 (容器状态/引擎码)"
    )
    error: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因 (SUCCESS 时为空)"
    )
    elapsed_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Docker API 往返耗时 (毫秒)"
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="批准/拒绝时刻"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class KnowledgeEntry(Base):
    """Phase 15: 运维知识库 —— BM25 检索语料 (手册 RUNBOOK / 闭环复盘 CASE / 文档片段 DOC)。

    零 fake 纪律:
    - source_type=CLOSED_LOOP 的复盘条目只能由**真实闭环数据**组装
      (归档 + SUCCESS 分析 + 执行单据 + 审计事件), 缺环直接拒绝生成;
    - source_type=MANUAL 是人写的知识, 写入/修改/删除全部进审计链
      (knowledge.created/updated/deleted) —— 知识库自身也是被审计对象。
    - problem_record_id 仅复盘条目回填, 用于时间线互查与"一问题一复盘"去重。
    """

    __tablename__ = "knowledge_entries"
    __table_args__ = (
        Index("ix_knowledge_category", "category"),
        Index("ix_knowledge_problem", "problem_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(
        String(16), nullable=False, default="DOC", comment="RUNBOOK / CASE / DOC"
    )
    source_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="MANUAL",
        comment="MANUAL=人手写 / CLOSED_LOOP=真实闭环自动生成",
    )
    problem_record_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("problem_records.id"), nullable=True,
        comment="复盘条目对应的归档问题; 手写知识为空",
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="逗号分隔, 参与检索加权"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
