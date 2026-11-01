"""Phase 13: 可执行操作目录 —— 全系统唯一能"动真实世界"的入口清单。

安全边界 (可核验的设计, 不是口头承诺):
- 操作不是动态生成的: OPERATIONS 是代码里写死的静态元组, LLM/接口都
  **没有**新增或修改条目的能力 (没有任何写目录的代码路径)。
- 每条操作只有 action + target 两个执行要素, action 必须落在
  ALLOWED_ACTIONS 固定集合里, target 必须落在 EXEC_ALLOW_TARGETS 白名单里;
  任何一层校验不过的操作直接不可见 (被过滤, 不是被拒绝后仍可执行)。
- 目录与 AI 工具白名单 (ai_agent.ALL_TOOL_SCHEMAS) 完全分离:
  AI 侧 8 个工具恒只读, 这里 0 个工具对 AI 开放。
- 触发匹配 (match_for_problem) 只做"该不该给人看一眼建议",
  生成的是 PENDING_APPROVAL 请求, 离执行还隔着一道人工审批。
"""

import logging
import re
from dataclasses import dataclass

from app.config import get_settings

logger = logging.getLogger("zops.services.exec_catalog")

# Docker 引擎层只放行这三个幂等动作; 任何 shell/组合命令在这里就不存在
ALLOWED_ACTIONS = frozenset({"restart", "start", "stop"})


@dataclass(frozen=True)
class Operation:
    """一条白名单操作: 执行要素只有 action + target, 没有自由参数。"""

    op_id: str
    name: str
    action: str            # 必须 ∈ ALLOWED_ACTIONS
    target: str            # 容器名, 必须 ∈ EXEC_ALLOW_TARGETS
    trigger_pattern: str   # 自动建议匹配: 命中归档触发器名才生成建议
    description: str
    risk: str


# 静态目录: 初期只放演练容器的重启恢复 —— 与 Phase 8~12 的演练闭环严丝合缝。
# 扩充操作 = 改这个元组 + 走代码评审, 刻意保留"加能力很麻烦"的摩擦。
OPERATIONS: tuple[Operation, ...] = (
    Operation(
        op_id="restart_zops_drill",
        name="重启演练容器 zops-drill",
        action="restart",
        target="zops-drill",
        trigger_pattern=r"zops-drill",
        description=(
            "对应 'ZOPS演练: 容器 zops-drill 已停止' 类告警: "
            "重启演练容器使服务恢复, Zabbix 触发器自动恢复、归档转 RECOVERED。"
        ),
        risk="低: 仅作用于演练容器自身; 核心 5 容器 (pg/server/web/agent/backend) 不在任何白名单。",
    ),
)


def allowed_targets() -> list[str]:
    """解析 EXEC_ALLOW_TARGETS (逗号分隔)。空配置 = 全部操作不可见。"""
    raw = get_settings().exec_allow_targets
    return [t.strip() for t in raw.split(",") if t.strip()]


def _is_executable(op: Operation) -> bool:
    """双重校验: 动作在固定集合内 且 目标在环境白名单内, 缺一即不可见。"""
    if op.action not in ALLOWED_ACTIONS:
        logger.warning("操作 %s 的 action=%s 不在固定集合, 已隐藏", op.op_id, op.action)
        return False
    if op.target not in allowed_targets():
        # 环境没授权这个容器 —— 目录里有定义也不会被执行, 直接不可见
        return False
    return True


def visible_operations() -> list[Operation]:
    """当前配置下真正可执行的操作 (GET /exec/operations 回显的就是它)。"""
    return [op for op in OPERATIONS if _is_executable(op)]


def get_operation(op_id: str) -> Operation | None:
    """按 id 取目录条目 (不校验可见性; 可见性由调用方过 _is_executable)。"""
    return next((op for op in OPERATIONS if op.op_id == op_id), None)


def match_for_problem(trigger_name: str) -> list[Operation]:
    """归档触发器名 → 命中的可见操作 (自动建议用; 只读匹配, 不产生执行)。"""
    hits: list[Operation] = []
    for op in visible_operations():
        try:
            if re.search(op.trigger_pattern, trigger_name):
                hits.append(op)
        except re.error:
            logger.warning("操作 %s 的 trigger_pattern 非法, 跳过", op.op_id)
    return hits
