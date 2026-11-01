"""API 路由聚合: 业务路由统一挂 /api/v1 前缀。

新增业务模块只改这里注册, main.py 不再逐个 import 路由。
"""

from fastapi import APIRouter

from app.api.routes import (
    analysis,
    audit,
    docker,
    execution,
    hosts,
    knowledge,
    problems,
    system,
    zabbix,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system.router)
api_router.include_router(zabbix.router)
api_router.include_router(hosts.router)
api_router.include_router(docker.router)
api_router.include_router(problems.router)
api_router.include_router(analysis.router)
api_router.include_router(execution.router)
api_router.include_router(audit.router)
api_router.include_router(knowledge.router)
