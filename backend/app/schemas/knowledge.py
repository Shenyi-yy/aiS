"""/api/v1/knowledge* 的出入参模型 (Phase 15 RAG 知识库)。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeCreateIn(BaseModel):
    """POST /knowledge: 手工录入 RUNBOOK/DOC (复盘只能由闭环生成)。"""

    title: str = Field(min_length=2, max_length=255)
    content: str = Field(min_length=1, max_length=200000)
    category: str = Field(default="DOC", pattern="^(RUNBOOK|DOC)$")
    tags: str = Field(default="", max_length=255)


class KnowledgeUpdateIn(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    content: str | None = Field(default=None, max_length=200000)
    tags: str | None = Field(default=None, max_length=255)


class KnowledgeOut(BaseModel):
    """条目 DTO: 来源三元组 (category/source_type/problem_record_id) 必须可见,
    读的人才能分辨"真实闭环复盘"还是"手工文档"。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    category: str
    source_type: str
    problem_record_id: int | None
    content: str
    tags: str
    created_at: datetime
    updated_at: datetime


class KnowledgePage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[KnowledgeOut]


class SearchHit(BaseModel):
    entry: KnowledgeOut
    score: float


class SearchOut(BaseModel):
    """GET /knowledge/search: BM25 命中列表 (score>0 才出现, 不硬凑)。"""

    query: str
    top_k: int
    corpus_total: int  # 语料规模如实回显, 空库就告诉你是空的
    count: int
    hits: list[SearchHit]


class CaseOut(BaseModel):
    """POST /knowledge/case/{problem_id}: 复盘条目 + 是否本次新建 (幂等证据)。"""

    entry: KnowledgeOut
    created: bool
