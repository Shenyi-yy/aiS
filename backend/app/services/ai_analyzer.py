"""Phase 9: 把"归档问题 + Zabbix 实时证据"喂给 LLM 做故障分析。

数据流 (两侧外部系统都只走各自唯一客户端, 本模块不直连任何 API):

    problem_records (Phase 5 真实归档)
          +  zabbix_client.fetch_event_detail() (Zabbix 侧原始事件对质)
          →  证据 JSON → llm_client.chat() → ai_analysis_records 落库

诚实原则: 调用失败也落一行 FAILED (含证据快照与错误原因) 再把异常抛给
路由层呈现 502 —— 大盘上永远分得清"AI 真说了话"和"AI 没说了话"。
"""

import json
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AIAnalysisRecord, ProblemRecord
from app.services import audit
from app.services.llm_client import LLMError, get_default_llm_client
from app.services.zabbix_client import ZabbixError, get_default_client

logger = logging.getLogger("zops.services.ai_analyzer")

SYSTEM_PROMPT = (
    "你是 ZOPS 智能运维平台的资深 SRE 值班专家。你会收到一个生产告警工单的"
    "JSON 证据包, 请只依据证据输出 Markdown 分析, 严格按以下四节, 不要客套:\n"
    "## 现象摘要\n## 可能原因 (按可能性排序)\n## 证据\n## 建议的下一步排查与处置\n"
    "硬性要求: 不得虚构任何指标数值或时间; 证据不足以定位时, 直接写明缺什么数据、"
    "应补采哪些监控项。建议的处置命令必须标注风险等级, 供人工确认 (本平台禁止"
    "AI 直接执行命令)。"
)


def build_evidence(problem: ProblemRecord) -> dict[str, Any]:
    """组装喂给模型的证据包: 归档行 + Zabbix 原始事件 + raw_payload。"""
    evidence: dict[str, Any] = {
        "problem_record": {
            "id": problem.id,
            "zabbix_eventid": problem.zabbix_eventid,
            "host": problem.host,
            "trigger_name": problem.trigger_name,
            "severity": problem.severity,
            "status": problem.status,
            "started_at": problem.started_at.isoformat(),
            "recovered_at": (
                problem.recovered_at.isoformat() if problem.recovered_at else None
            ),
        }
    }
    # Zabbix 侧回查对质: 归档可能丢细节; 查不到不阻塞分析, 但要在证据里
    # 写明缺口, 让模型自己知道"这部分证据没了"而不是编出来。
    try:
        event = get_default_client().fetch_event_detail(problem.zabbix_eventid)
        evidence["zabbix_event"] = {
            "eventid": event.get("eventid"),
            "name": event.get("name"),
            "clock": event.get("clock"),
            "value": event.get("value"),
            "severity": event.get("severity"),
            "hosts": event.get("hosts"),
        }
    except ZabbixError as exc:
        evidence["zabbix_event"] = {"unavailable": str(exc)}
    if problem.raw_payload:
        try:
            evidence["raw_event_payload"] = json.loads(problem.raw_payload)
        except ValueError:
            evidence["raw_event_payload"] = problem.raw_payload[:2000]
    return evidence


def analyze_problem(db: Session, problem_id: int) -> AIAnalysisRecord:
    """对一个真实归档问题跑一次 LLM 分析; SUCCESS/FAILED 都落库。

    抛 LookupError (无此问题) / LLMError (LLM 侧失败), 路由层负责 HTTP 语义。
    """
    problem = db.get(ProblemRecord, problem_id)
    if problem is None:
        raise LookupError(
            f"problem_records 没有 id={problem_id} 的归档 —— "
            "先 POST /api/v1/problems/sync 同步真实告警再来分析"
        )

    evidence = build_evidence(problem)
    evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "告警工单证据包 (JSON):\n" + evidence_json},
    ]

    llm = get_default_llm_client()
    record = AIAnalysisRecord(
        problem_record_id=problem.id,
        host=problem.host,
        trigger_name=problem.trigger_name,
        model=llm.model,
        input_snapshot=evidence_json,
    )
    start = time.monotonic()
    try:
        content, usage = llm.chat(messages)
    except LLMError as exc:
        record.status = "FAILED"
        record.error = str(exc)
        record.elapsed_ms = int((time.monotonic() - start) * 1000)
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.warning("AI 分析失败已归档 FAILED: problem=%s err=%s", problem_id, exc)
        # Phase 14: 失败同样进审计链 —— "AI 没说话"也是要说清的事实
        audit.record_event(
            db=db,
            event_type=audit.EV_ANALYSIS_FAILED,
            actor="llm-analyzer", source="API",
            object_type="ai_analysis_record", object_id=record.id,
            link_problem_id=problem.id,
            detail={
                "model": record.model, "elapsed_ms": record.elapsed_ms,
                "error": str(exc)[:500], "trigger_source": "MANUAL",
            },
        )
        raise

    record.status = "SUCCESS"
    record.analysis = content
    record.prompt_tokens = int(usage.get("prompt_tokens") or 0)
    record.completion_tokens = int(usage.get("completion_tokens") or 0)
    record.elapsed_ms = int((time.monotonic() - start) * 1000)
    db.add(record)
    db.commit()
    db.refresh(record)
    logger.info(
        "AI 分析完成: problem=%s model=%s elapsed=%sms tokens=%s/%s",
        problem_id,
        record.model,
        record.elapsed_ms,
        record.prompt_tokens,
        record.completion_tokens,
    )
    # Phase 14: 成功进审计链 (分析记录 id + 问题关联, 时间线可回放)
    audit.record_event(
        db=db,
        event_type=audit.EV_ANALYSIS_SUCCESS,
        actor="llm-analyzer", source="API",
        object_type="ai_analysis_record", object_id=record.id,
        link_problem_id=problem.id,
        detail={
            "model": record.model, "elapsed_ms": record.elapsed_ms,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "trigger_source": "MANUAL",
        },
    )
    return record
