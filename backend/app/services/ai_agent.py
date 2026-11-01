"""Agent 工具循环 —— 大模型自主取证再下结论 (P10 Zabbix 组, P11 Docker/Linux 组, P15 知识库组)。

与 Phase 9 的区别: Phase 9 是人喂证据, 本模块是模型**自己调工具取证**:

    工单证据(同 P9) + 知识库检索(P15, round=0 证据注入) + tools 白名单
    (4 zbx_* + 4 docker_*/host_* + 1 kb_search)
      → chat_raw → tool_calls? → dispatch_tool(观察回填 messages) → 再问
      → 直到模型给出最终正文 (或超出 LLM_AGENT_MAX_ROUNDS 抛 LLMError)

安全边界: 三组注册表**都只有只读工具**, 写操作/命令执行在代码结构上不存在;
docker.sock 也没挂给后端 —— 容器数据一律走 agent2 已采集进 Zabbix 的实采值。
循环上限防失控; 全过程 tool_trace 落库 (P14 审计链)。
"""

import json
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AIAnalysisRecord, ProblemRecord
from app.services import audit
from app.services import knowledge as knowledge_service
from app.services.ai_analyzer import SYSTEM_PROMPT, build_evidence
from app.services.host_tools import HOST_TOOL_SCHEMAS
from app.services.host_tools import execute_tool as execute_host_tool
from app.services.host_tools import list_tool_names as list_host_tool_names
from app.services.knowledge_tools import KB_TOOL_SCHEMAS
from app.services.knowledge_tools import execute_tool as execute_kb_tool
from app.services.knowledge_tools import list_tool_names as list_kb_tool_names
from app.services.llm_client import LLMError, get_default_llm_client
from app.services.zabbix_tools import TOOL_SCHEMAS, execute_tool
from app.services.zabbix_tools import list_tool_names as list_zbx_tool_names

logger = logging.getLogger("zops.services.ai_agent")

_TOOL_RESULT_MAX = 4000  # 单次观察回填的字符上限, 防爆上下文

# 三组只读工具合并 (P10 zbx / P11 docker+host / P15 kb): 模型看到的是一张白名单
ALL_TOOL_SCHEMAS: list[dict[str, Any]] = TOOL_SCHEMAS + HOST_TOOL_SCHEMAS + KB_TOOL_SCHEMAS
_ALL_TOOL_NAMES = list_zbx_tool_names() + list_host_tool_names() + list_kb_tool_names()


def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """按工具名分派到对应注册表; 白名单外一律拒绝 (三组共用同一道闸)。"""
    if name in _ALL_TOOL_NAMES:
        if name in list_kb_tool_names():
            return execute_kb_tool(name, args)
        if name in list_host_tool_names():
            return execute_host_tool(name, args)
        return execute_tool(name, args)
    return {"error": f"工具 {name} 不存在或不在只读白名单内"}


def run_agent(
    llm: Any,
    messages: list[dict[str, Any]],
    max_rounds: int,
) -> tuple[str, list[dict[str, Any]]]:
    """多轮 tool-calling 循环, 返回 (最终正文, 调用轨迹)。

    测试可注入假 llm —— 轨迹形状与生产一致。
    """
    trace: list[dict[str, Any]] = []
    for rnd in range(1, max_rounds + 1):
        payload = llm.chat_raw(messages, tools=ALL_TOOL_SCHEMAS)
        msg = payload["choices"][0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or ""), trace
        # OpenAI 协议要求: assistant 的 tool_calls 消息原样回填, 再跟 tool 观察
        messages.append(
            {
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": calls,
            }
        )
        for call in calls:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            t0 = time.monotonic()
            observation = dispatch_tool(name, args)
            tool_ms = int((time.monotonic() - t0) * 1000)
            obs_text = json.dumps(observation, ensure_ascii=False, default=str)
            trace.append(
                {
                    "round": rnd,
                    "tool": name,
                    "args": args,
                    "ok": "error" not in observation,
                    "result_head": obs_text[:200],
                    "tool_ms": tool_ms,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": obs_text[:_TOOL_RESULT_MAX],
                }
            )
    err = LLMError(
        f"Agent 达到最大工具轮数 {max_rounds} 仍未得出结论 —— 已中止 (防失控)。"
        " 可在 /opt/zops/.env 调大 LLM_AGENT_MAX_ROUNDS 后重建 backend"
    )
    err.trace = trace  # 把已发生的调用轨迹带出去, FAILED 也留证据
    raise err


def analyze_problem_with_tools(
    db: Session, problem_id: int, trigger_source: str = "MANUAL"
) -> AIAnalysisRecord:
    """带工具取证的分析; SUCCESS/FAILED 都落库 (含 tool_trace)。

    Phase 12: trigger_source=MANUAL/AUTO —— 后台调度传 AUTO, 路由默认 MANUAL,
    审计 (P14) 据此区分"谁让模型跑的"。
    """
    problem = db.get(ProblemRecord, problem_id)
    if problem is None:
        raise LookupError(
            f"problem_records 没有 id={problem_id} 的归档 —— "
            "先 POST /api/v1/problems/sync 同步真实告警再来分析"
        )

    evidence = build_evidence(problem)
    evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)

    # Phase 15 检索通道一: 分析前先从知识库 BM25 检索, 命中才注入 (零命中
    # 不硬塞参考, 也不假装查过)。检索失败绝不阻塞分析 —— 只记缺口。
    rag_query = f"{problem.trigger_name} {problem.host}"
    rag_t0 = time.monotonic()
    rag_hits: list[Any] = []
    rag_ok = True
    rag_error = ""
    try:
        rag_hits = knowledge_service.retrieve(db, rag_query, top_k=2)
    except Exception as exc:  # noqa: BLE001 - 知识库炸了也要继续分析
        rag_ok = False
        rag_error = str(exc)[:200]
        logger.exception("知识库检索失败 (分析继续, 无参考注入)")
    rag_trace: dict[str, Any] = {
        "round": 0,
        "tool": "rag_retrieve",
        "args": {"query": rag_query, "top_k": 2},
        "ok": rag_ok,
        "result_head": (
            " / ".join(e.title for e, _ in rag_hits) if rag_hits
            else "(无命中)" if rag_ok else f"(检索失败: {rag_error})"
        )[:200],
        "tool_ms": int((time.monotonic() - rag_t0) * 1000),
        "hits": len(rag_hits),
    }

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "system",
            "content": (
                "你可以调用三组只读工具向监控面补充取证: zbx_* (Zabbix 事件/监控项/触发器) "
                "、docker_*/host_* (容器清单与详情/宿主机 Linux 指标, 数据来自 agent2 实采) "
                "、kb_search (运维知识库检索)。请先取证再下结论; 证据不足要明说缺什么。"
            ),
        },
    ]
    if rag_hits:
        refs = "\n\n".join(
            f"【{e.category}·{e.source_type}】{e.title}\n{(e.content or '')[:1200]}"
            for e, _ in rag_hits
        )
        messages.append(
            {
                "role": "system",
                "content": (
                    "知识库检索参考 (来自历史复盘/运维文档, 不是本次告警的实时证据, "
                    "与当前监控面数据冲突时以证据为先, 引用请注明来源): \n\n" + refs
                ),
            }
        )
    messages.append({"role": "user", "content": "告警工单证据包 (JSON):\n" + evidence_json})

    llm = get_default_llm_client()
    settings = get_settings()
    record = AIAnalysisRecord(
        problem_record_id=problem.id,
        host=problem.host,
        trigger_name=problem.trigger_name,
        model=llm.model,
        input_snapshot=evidence_json,
        trigger_source=trigger_source,
    )
    start = time.monotonic()
    try:
        content, trace = run_agent(
            llm, messages, max_rounds=settings.llm_agent_max_rounds
        )
    except LLMError as exc:
        record.status = "FAILED"
        record.error = str(exc)
        # round=0 的 rag_retrieve 也进 trace —— 失败归档要完整反映"做过什么"
        record.tool_trace = json.dumps(
            [rag_trace] + list(getattr(exc, "trace", [])), ensure_ascii=False
        )
        record.elapsed_ms = int((time.monotonic() - start) * 1000)
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.warning("Agent 分析失败已归档 FAILED: problem=%s err=%s", problem_id, exc)
        # Phase 14: 失败入链, detail 带已发生的工具调用数 (取证到哪一步炸的)
        audit.record_event(
            db=db,
            event_type=audit.EV_ANALYSIS_FAILED,
            actor="llm-agent", source="API" if trigger_source == "MANUAL" else "AUTO",
            object_type="ai_analysis_record", object_id=record.id,
            link_problem_id=problem.id,
            detail={
                "model": record.model, "elapsed_ms": record.elapsed_ms,
                "error": str(exc)[:500], "trigger_source": trigger_source,
                "tool_calls": len(json.loads(record.tool_trace or "[]")),
            },
        )
        raise
    record.status = "SUCCESS"
    record.analysis = content
    full_trace = [rag_trace] + trace
    record.tool_trace = json.dumps(full_trace, ensure_ascii=False)
    record.elapsed_ms = int((time.monotonic() - start) * 1000)
    db.add(record)
    db.commit()
    db.refresh(record)
    logger.info(
        "Agent 分析完成: problem=%s 工具调用=%s 次 (含 rag_retrieve) elapsed=%sms",
        problem_id,
        len(full_trace),
        record.elapsed_ms,
    )
    # Phase 14: 成功入链; trigger_source 保留 —— "谁让模型跑的"必须可审
    audit.record_event(
        db=db,
        event_type=audit.EV_ANALYSIS_SUCCESS,
        actor="llm-agent", source="API" if trigger_source == "MANUAL" else "AUTO",
        object_type="ai_analysis_record", object_id=record.id,
        link_problem_id=problem.id,
        detail={
            "model": record.model, "elapsed_ms": record.elapsed_ms,
            "trigger_source": trigger_source, "tool_calls": len(full_trace),
            "tools_used": sorted({t["tool"] for t in full_trace}),
        },
    )
    return record
