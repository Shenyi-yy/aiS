"""Phase 15: RAG 知识库 —— BM25 纯 Python 检索 + 真实闭环复盘生成。

为什么是 BM25 而不是向量库 (诚实优先):
- VM 到 embedding 服务的网络/凭据链路未验证 (这台 VM 连 pip 官方源都抖),
  拿没验证过的依赖做核心链路 = 演示翻车风险;
- BM25 是确定性的经典检索算法 (Okapi BM25, 与 Elasticsearch 默认打分同源),
  零外部依赖、离线可复现、结果可解释 (命中词/得分都摆得出来);
- 语料是运维文档量级 (几十~几百条), 每次查询从库里现算, 没有缓存一致性坑。

中文分词用最稳的 bigram (双字滑窗) + ASCII 词, 不引任何分词库。

零 fake 纪律: build_case_document 只从真实闭环行 (归档 + SUCCESS 分析 +
执行单据 + 审计事件) 组装复盘; 缺分析记录直接 KnowledgeUnready 拒绝。
知识库的写操作全部进 P14 审计链 (knowledge.created/updated/deleted)。
"""

import logging
import math
import re
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AIAnalysisRecord,
    AuditEvent,
    ExecutionRecord,
    KnowledgeEntry,
    ProblemRecord,
)
from app.services import audit

logger = logging.getLogger("zops.services.knowledge")

_K1 = 1.5
_B = 0.75

_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_.\-/]*")
_CJK_RUN = re.compile(r"[一-鿿]+")


class KnowledgeError(Exception):
    """基类: 路由层按子类映射 HTTP 语义。"""


class KnowledgeNotFound(KnowledgeError):
    """条目/问题不存在 → 404。"""


class KnowledgeUnready(KnowledgeError):
    """闭环不完整, 拒绝生成复盘 → 409。"""


def tokenize(text: str) -> list[str]:
    """ASCII 小写词 + 中文 bigram。无库、确定性、可单测。"""
    low = (text or "").lower()
    toks = _ASCII_WORD.findall(low)
    for run in _CJK_RUN.findall(low):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i : i + 2] for i in range(len(run) - 1))
    return toks


def _doc_vector(entry: KnowledgeEntry) -> tuple[int, Counter, int]:
    # tags 重复一次: 人贴的标签是显式相关性信号, 相当于加权
    text = f"{entry.title} {entry.content} {entry.tags} {entry.tags}"
    toks = tokenize(text)
    return entry.id, Counter(toks), len(toks)


def retrieve(db: Session, query: str, top_k: int = 3) -> list[tuple[KnowledgeEntry, float]]:
    """全库 BM25 打分取前 top_k (score<=0 的不算命中 —— 不硬凑结果)。"""
    rows = db.scalars(select(KnowledgeEntry)).all()
    if not rows:
        return []
    q_toks = set(tokenize(query))
    docs = [_doc_vector(r) for r in rows]
    avgdl = sum(d[2] for d in docs) / len(docs) or 1.0
    df: Counter = Counter()
    for _, tf, _ in docs:
        for t in set(tf):
            df[t] += 1
    scores: dict[int, float] = {}
    n = len(docs)
    for t in q_toks:
        if df[t] == 0:
            continue
        idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
        for eid, tf, dl in docs:
            f = tf.get(t, 0)
            if f:
                scores[eid] = scores.get(eid, 0.0) + idf * f * (_K1 + 1) / (
                    f + _K1 * (1 - _B + _B * dl / avgdl)
                )
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    by_id = {r.id: r for r in rows}
    return [(by_id[eid], s) for eid, s in ranked if s > 0]


def create_entry(
    db: Session,
    *,
    title: str,
    content: str,
    category: str = "DOC",
    tags: str = "",
    source_type: str = "MANUAL",
    problem_record_id: int | None = None,
) -> KnowledgeEntry:
    row = KnowledgeEntry(
        title=title.strip()[:255],
        content=content,
        category=category,
        tags=tags.strip()[:255],
        source_type=source_type,
        problem_record_id=problem_record_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    audit.record_event(
        db=db,
        event_type=audit.EV_KNOWLEDGE_CREATED,
        actor="api-client", source="API",
        object_type="knowledge_entry", object_id=row.id,
        link_problem_id=problem_record_id,
        detail={
            "title": row.title[:120], "category": row.category,
            "source_type": row.source_type, "content_len": len(row.content),
        },
    )
    logger.info("知识库新增 id=%s category=%s source=%s", row.id, row.category, row.source_type)
    return row


def get_entry(db: Session, entry_id: int) -> KnowledgeEntry:
    row = db.get(KnowledgeEntry, entry_id)
    if row is None:
        raise KnowledgeNotFound(f"knowledge_entries 没有 id={entry_id}")
    return row


def update_entry(
    db: Session,
    entry_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    tags: str | None = None,
) -> KnowledgeEntry:
    row = get_entry(db, entry_id)
    before = {"title": row.title, "content_len": len(row.content), "tags": row.tags}
    if title is not None and title.strip():
        row.title = title.strip()[:255]
    if content is not None and content.strip():
        row.content = content
    if tags is not None:
        row.tags = tags.strip()[:255]
    db.commit()
    db.refresh(row)
    audit.record_event(
        db=db,
        event_type=audit.EV_KNOWLEDGE_UPDATED,
        actor="api-client", source="API",
        object_type="knowledge_entry", object_id=row.id,
        link_problem_id=row.problem_record_id,
        detail={"before": before, "after": {
            "title": row.title[:120], "content_len": len(row.content),
            "tags": row.tags,
        }},
    )
    return row


def delete_entry(db: Session, entry_id: int) -> int:
    row = get_entry(db, entry_id)
    rid = row.id
    audit.record_event(
        db=db,
        event_type=audit.EV_KNOWLEDGE_DELETED,
        actor="api-client", source="API",
        object_type="knowledge_entry", object_id=rid,
        link_problem_id=row.problem_record_id,
        detail={"title": row.title[:120], "category": row.category,
                "source_type": row.source_type},
    )
    db.delete(row)
    db.commit()
    logger.info("知识库删除 id=%s (删除动作本身已在审计链)", rid)
    return rid


def _fmt(dt: Any) -> str:
    return dt.isoformat() if dt is not None else "-"


def build_case_document(db: Session, problem_id: int) -> tuple[KnowledgeEntry, bool]:
    """从真实闭环组装复盘条目; 返回 (条目, 是否本次新建)。

    幂等: 一个归档问题只有一条 CLOSED_LOOP 复盘, 第二次调用原样返回旧的
    —— verify 脚本重复跑不产生重复语料, 也不刷审计。
    拒绝: 问题不存在 404; 没有 SUCCESS 分析 409 (缺环就不写, 不写想象中的复盘)。
    """
    problem = db.get(ProblemRecord, problem_id)
    if problem is None:
        raise KnowledgeNotFound(
            f"problem_records 没有 id={problem_id} —— 复盘只从真实归档生成"
        )
    existing = db.scalars(
        select(KnowledgeEntry).where(
            KnowledgeEntry.problem_record_id == problem.id,
            KnowledgeEntry.source_type == "CLOSED_LOOP",
        )
    ).first()
    if existing is not None:
        return existing, False
    analysis = db.scalars(
        select(AIAnalysisRecord)
        .where(
            AIAnalysisRecord.problem_record_id == problem.id,
            AIAnalysisRecord.status == "SUCCESS",
        )
        .order_by(AIAnalysisRecord.id.desc())
    ).first()
    if analysis is None:
        raise KnowledgeUnready(
            f"问题 {problem.id} 没有 SUCCESS 分析记录 —— 复盘必须基于真实发生过的"
            "分析与执行闭环, 缺环不生成 (先手工分析或等自动分析完成)"
        )
    execs = db.scalars(
        select(ExecutionRecord)
        .where(ExecutionRecord.problem_record_id == problem.id)
        .order_by(ExecutionRecord.id.asc())
    ).all()
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.link_problem_id == problem.id)
        .order_by(AuditEvent.id.asc())
    ).all()

    lines: list[str] = [
        "# 故障闭环复盘 (ZOPS 自动生成, 全部来自真实记录)",
        "",
        "## 告警",
        f"- 归档 id={problem.id} zabbix_eventid={problem.zabbix_eventid}",
        f"- 主机: {problem.host}",
        f"- 触发器: {problem.trigger_name} (severity={problem.severity})",
        f"- 时间: 产生 {_fmt(problem.started_at)} / 恢复 {_fmt(problem.recovered_at)}"
        f" / 当前状态 {problem.status}",
        "",
        "## AI 分析 (记录 id=%s, 来源 %s)" % (analysis.id, analysis.trigger_source),
        f"- 模型: {analysis.model}; 耗时 {analysis.elapsed_ms}ms; 工具链见 tool_trace",
        "",
        (analysis.analysis or "(空)").strip()[:4000],
        "",
        "## 处置 (人工审批通道)",
    ]
    if execs:
        for r in execs:
            lines.append(
                f"- 单据 {r.id} [{r.source}] {r.action} {r.target} → {r.status}; "
                f"审批人: {r.approver or '-'}; 决定于 {_fmt(r.decided_at)}; "
                f"结果: {(r.result or r.error or '-')[:200]}"
            )
    else:
        lines.append("- 无执行单据 (本次故障未走到处置环节, 如实记录)")
    lines += ["", "## 审计链事件 (P14, 证据不可改)"]
    for ev in events:
        lines.append(
            f"- #{ev.id} {ev.event_type} source={ev.source} actor={ev.actor or '-'} "
            f"@ {_fmt(ev.occurred_at)}"
        )
    entry = create_entry(
        db,
        title=f"闭环复盘: {problem.trigger_name}",
        content="\n".join(lines),
        category="CASE",
        tags=f"{problem.host},{problem.trigger_name[:80]}",
        source_type="CLOSED_LOOP",
        problem_record_id=problem.id,
    )
    logger.info("复盘生成: problem=%s -> knowledge id=%s", problem.id, entry.id)
    return entry, True
