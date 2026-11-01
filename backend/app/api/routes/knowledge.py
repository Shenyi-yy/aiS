"""知识库路由: /api/v1/knowledge* (Phase 15 RAG)

- POST   /knowledge                 手工录入 RUNBOOK/DOC (写操作, 入审计链)
- GET    /knowledge                 条目分页 (可按 category 过滤)
- GET    /knowledge/search          BM25 检索 (q 必填; 只回 score>0, 不硬凑)
- GET    /knowledge/{entry_id}      条目详情
- PUT    /knowledge/{entry_id}      修改 (before/after 入审计链)
- DELETE /knowledge/{entry_id}      删除 (删除动作本身先入链再删)
- POST   /knowledge/case/{problem_id}  从真实闭环生成复盘 (幂等; 缺环 409)

顺序说明: /search 与 /case 必须注册在 /{entry_id} 之前 —— FastAPI 按声明
顺序匹配, 否则 "search" 会被当成 entry_id 报 422。

诚实边界: 本路由不提供 category=CASE 的手工写入口 —— 复盘只能由
build_case_document 从真实归档/分析/执行/审计行组装。异常映射:
不存在=404 / 闭环缺环=409。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import KnowledgeEntry
from app.schemas.common import ApiResponse
from app.schemas.knowledge import (
    CaseOut,
    KnowledgeCreateIn,
    KnowledgeOut,
    KnowledgePage,
    KnowledgeUpdateIn,
    SearchHit,
    SearchOut,
)
from app.services.knowledge import (
    KnowledgeNotFound,
    KnowledgeUnready,
    build_case_document,
    create_entry,
    delete_entry,
    get_entry,
    retrieve,
    update_entry,
)

logger = logging.getLogger("zops.api.knowledge")

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("", response_model=ApiResponse[KnowledgeOut])
def create(
    body: KnowledgeCreateIn, db: Session = Depends(get_db)
) -> ApiResponse[KnowledgeOut]:
    """手工知识录入: category 只允许 RUNBOOK/DOC (schema 正则收口)。"""
    row = create_entry(
        db,
        title=body.title,
        content=body.content,
        category=body.category,
        tags=body.tags,
        source_type="MANUAL",
    )
    return ApiResponse(data=KnowledgeOut.model_validate(row))


@router.get("", response_model=ApiResponse[KnowledgePage])
def list_entries(
    db: Session = Depends(get_db),
    category: str | None = Query(default=None, pattern="^(RUNBOOK|CASE|DOC)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[KnowledgePage]:
    stmt = select(KnowledgeEntry)
    if category is not None:
        stmt = stmt.where(KnowledgeEntry.category == category)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(KnowledgeEntry.id.desc()).offset(offset).limit(limit)
    ).all()
    return ApiResponse(
        data=KnowledgePage(
            total=total, offset=offset, limit=limit,
            items=[KnowledgeOut.model_validate(r) for r in rows],
        )
    )


@router.get("/search", response_model=ApiResponse[SearchOut])
def search(
    db: Session = Depends(get_db),
    q: str = Query(min_length=1, max_length=500, description="检索词 (BM25)"),
    top_k: int = Query(default=3, ge=1, le=5),
) -> ApiResponse[SearchOut]:
    """检索知识条目; corpus_total 如实回显语料规模, 零命中 count=0。"""
    hits = retrieve(db, q, top_k=top_k)
    corpus = db.scalar(select(func.count()).select_from(KnowledgeEntry)) or 0
    return ApiResponse(
        data=SearchOut(
            query=q, top_k=top_k, corpus_total=corpus, count=len(hits),
            hits=[
                SearchHit(entry=KnowledgeOut.model_validate(e), score=round(s, 3))
                for e, s in hits
            ],
        )
    )


@router.post("/case/{problem_id}", response_model=ApiResponse[CaseOut])
def build_case(
    problem_id: int, db: Session = Depends(get_db)
) -> ApiResponse[CaseOut]:
    """从真实闭环生成复盘 (幂等: 已有则原样返回, created=false)。"""
    try:
        entry, created = build_case_document(db, problem_id)
    except KnowledgeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KnowledgeUnready as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(
        data=CaseOut(entry=KnowledgeOut.model_validate(entry), created=created)
    )


@router.get("/{entry_id}", response_model=ApiResponse[KnowledgeOut])
def read_entry(
    entry_id: int, db: Session = Depends(get_db)
) -> ApiResponse[KnowledgeOut]:
    try:
        row = get_entry(db, entry_id)
    except KnowledgeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data=KnowledgeOut.model_validate(row))


@router.put("/{entry_id}", response_model=ApiResponse[KnowledgeOut])
def update(
    entry_id: int, body: KnowledgeUpdateIn, db: Session = Depends(get_db)
) -> ApiResponse[KnowledgeOut]:
    try:
        row = update_entry(
            db, entry_id, title=body.title, content=body.content, tags=body.tags
        )
    except KnowledgeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data=KnowledgeOut.model_validate(row))


@router.delete("/{entry_id}", response_model=ApiResponse[int])
def remove(
    entry_id: int, db: Session = Depends(get_db)
) -> ApiResponse[int]:
    try:
        rid = delete_entry(db, entry_id)
    except KnowledgeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data=rid)
