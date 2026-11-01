"""Phase 15: 知识库只读检索工具 (Agent 的第三组手, 也是唯一第 9 个工具)。

纪律与前两组完全一致:
- 只有 kb_search 一个只读工具; 知识库的写 (增删改/生成复盘) 只走 API 路由,
  模型侧结构上不存在改库的路径 —— AI 不执行危险动作的红线不动。
- 任何异常消化成 {"error": ...} 观察, 不崩 agent。
- 返回瘦身: 命中条目只回标题/类别/得分 + 正文摘录 (≤600 字符), top_k 封顶 5。

数据源: knowledge_service.retrieve 的 BM25 打分, 语料是真实归档生成的
复盘 + 手工录入的 RUNBOOK/DOC; 不命中的查询诚实返回空, 不硬凑。
"""

import logging
from typing import Any, Callable

from app.db.base import SessionLocal
from app.services import knowledge as knowledge_service

logger = logging.getLogger("zops.services.knowledge_tools")

_MAX_TOP_K = 5
_EXCERPT = 600


def _kb_search(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "参数缺失: query 必须是非空检索词 (如 'zops-drill 容器 停止')"}
    try:
        top_k = int(args.get("top_k") or 3)
    except (TypeError, ValueError):
        top_k = 3
    top_k = max(1, min(top_k, _MAX_TOP_K))
    db = SessionLocal()
    try:
        hits = knowledge_service.retrieve(db, query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 - 工具观察不外抛
        logger.exception("kb_search 检索失败")
        return {"error": f"知识库检索失败: {exc}"}
    finally:
        db.close()
    return {
        "query": query,
        "count": len(hits),
        "hits": [
            {
                "id": entry.id,
                "title": entry.title,
                "category": entry.category,
                "source_type": entry.source_type,
                "score": round(score, 3),
                "excerpt": entry.content[:_EXCERPT]
                + ("...(截断)" if len(entry.content) > _EXCERPT else ""),
            }
            for entry, score in hits
        ],
    }


_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "kb_search": _kb_search,
}

KB_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "kb_search",
            "description": (
                "检索运维知识库 (BM25), 返回最相关条目的标题/类别/得分与正文摘录。"
                "语料来自真实闭环复盘 (CASE) 与手工 RUNBOOK/DOC; 只读, 无命中时诚实返回空。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索词, 如 'zops-drill 容器 已停止' 或 '磁盘水位 清理'",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回前几条 (默认 3, 上限 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def list_tool_names() -> list[str]:
    return sorted(_HANDLERS)


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行知识库组工具; 白名单外一律变观察字典。"""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"工具 {name} 不存在或不在只读白名单内"}
    try:
        return handler(args or {})
    except Exception as exc:  # noqa: BLE001
        logger.exception("知识库工具执行异常: %s", name)
        return {"error": f"工具 {name} 执行异常: {exc}"}
