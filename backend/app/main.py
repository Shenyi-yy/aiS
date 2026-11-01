"""ZOPS 智能运维平台 - FastAPI 后端入口 (Phase 16: 平台完善)

本阶段结构:
- app/config.py      配置 (环境变量 / .env, 无硬编码敏感值)
- app/db/            SQLAlchemy 2.0 基础设施 + 业务模型 (+ knowledge_entries 表)
- app/schemas/       Pydantic 出入参
- app/services/      Zabbix API 客户端 (唯一出口) + LLM 客户端 (唯一出口)
                     + 问题归档 / Docker 监控聚合 / AI 故障分析 / 自动分析调度
                     + 执行目录与审批状态机 / Docker Engine 极简执行器 (P13)
                     + 审计哈希链服务, 只追加 (P14)
                     + BM25 知识库检索 + 闭环复盘生成 (P15)
- app/api/routes/    路由层 (health / system / zabbix / hosts / docker /
                     problems / ai / exec / audit[仅 GET] / knowledge)
- app/api/router.py  业务路由聚合, 统一 /api/v1 前缀
- app/static/        原生 JS 单页监控大盘 (零构建链)

大盘 (app/static) 已覆盖全闭环: 主机/Docker/归档/AI 分析/审批单/审计链/知识库检索/问题时间线。
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.config import get_settings
from app.db.base import init_db
from app.services.ai_dispatcher import auto_analyze_loop
from app.services.problem_sync import sync_once
from app.services.zabbix_client import ZabbixError
from app.version import APP_VERSION

settings = get_settings()

# ---------- 日志 ----------
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("zops.backend")


async def _zabbix_sync_loop() -> None:
    """周期把 Zabbix 真实问题事件归档进 problem_records。

    任何一次失败只记日志, 下一轮继续 —— 归档任务不能拖垮 API 服务。
    sync_once 是阻塞 IO (httpx/SQLite), 放线程池执行。
    """
    interval = max(settings.zabbix_sync_interval, 15)
    while True:
        try:
            result = await asyncio.to_thread(sync_once)
            if result.inserted or result.updated:
                logger.info(
                    "后台归档: fetched=%s 新增=%s 更新=%s",
                    result.fetched,
                    result.inserted,
                    result.updated,
                )
        except ZabbixError as exc:
            logger.warning("后台归档失败 (Zabbix 侧, %ss 后重试): %s", interval, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("后台归档异常 (%ss 后重试)", interval)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动: 建表 + (凭据就绪时) Zabbix 归档任务 + (开关开启时) AI 自动分析调度。"""
    init_db()
    task: asyncio.Task | None = None
    ai_task: asyncio.Task | None = None
    if settings.zabbix_password:
        task = asyncio.create_task(_zabbix_sync_loop(), name="zabbix-sync")
        logger.info(
            "Zabbix 归档任务已启动 (interval=%ss, days=%s)",
            max(settings.zabbix_sync_interval, 15),
            settings.zabbix_sync_days,
        )
    else:
        logger.warning(
            "ZABBIX_PASSWORD 未配置: Zabbix API 相关接口不可用, "
            "请在 Ubuntu 侧 /opt/zops/.env 填入后重启 backend"
        )
    if settings.ai_auto_enabled:
        # 双条件: 开关开启 且 LLM 三要素就绪 (llm.configured() 判定)。
        # 未配置时不拉起 —— 调度循环里也有兜底跳过, 双保险不造假忙碌。
        from app.services.llm_client import get_default_llm_client

        if get_default_llm_client().configured():
            ai_task = asyncio.create_task(
                auto_analyze_loop(), name="ai-auto-analyze"
            )
        else:
            logger.warning("LLM 未配置: AI 自动分析调度未启动 (手工接口不受影响)")
    else:
        logger.info("AI_AUTO_ENABLED=false: 自动分析调度未启动")
    # Phase 14: 进程自身入链 —— "这次启动开了哪些开关" 是可回溯的事实
    from app.services import audit
    from app.services.llm_client import get_default_llm_client as _llm_peek

    audit.record_event(
        event_type=audit.EV_SYSTEM_STARTUP,
        actor="system", source="SYSTEM",
        object_type="process", object_id=APP_VERSION,
        detail={
            "version": APP_VERSION,
            "env": settings.app_env,
            "zabbix_password_configured": bool(settings.zabbix_password),
            "zabbix_sync_task_started": task is not None,
            "ai_auto_task_started": ai_task is not None,
            "llm_configured": _llm_peek().configured(),
            "exec_enabled": settings.exec_enabled,
            "audit_enabled": settings.audit_enabled,
        },
    )
    logger.info(
        "ZOPS backend %s 启动完成 (env=%s, db=%s)",
        APP_VERSION,
        settings.app_env,
        settings.database_url.split("///")[-1],
    )
    yield
    for t in (task, ai_task):
        if t is not None:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t


app = FastAPI(
    title="ZOPS - 智能运维监控与故障分析平台",
    description="Phase 16: 平台完善 —— 大盘补齐闭环四面板(分析/审批单/审计/知识库)+问题时间线, 端到端巡检总闸 verify_phase16.sh",
    version=APP_VERSION,
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """全局异常兜底: 记录堆栈, 对外只返回通用错误, 不泄漏内部信息。"""
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


# 根路径存活检查 (docker healthcheck 依赖, 形状不变)
app.include_router(health_router)
# 业务接口统一 /api/v1
app.include_router(api_router)

# ---------- Phase 6: Web Dashboard ----------
# 静态大盘挂在 "/" , 必须注册在所有 API 路由之后:
# Starlette 按注册顺序匹配, 后挂载的 catch-all 不会抢走 /health 与 /api/v1/*。
_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="ui")
