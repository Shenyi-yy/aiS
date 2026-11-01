"""数据库基础设施 (Phase 4): SQLAlchemy 2.0 engine / session / 建表 / 健康检查

设计说明:
- ZOPS 业务库用 SQLite (容器内命名卷 backend_data 挂载 /app/data), 与 Zabbix 的
  PostgreSQL 完全分离: Zabbix 负责采集/告警/历史数据, 本库只存 ZOPS 自己的业务
  记录 (问题归档、后续阶段的审计日志等)。
- 所有连接串来自 Settings (环境变量 / .env), 禁止硬编码。
"""

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger("zops.db")

settings = get_settings()


class Base(DeclarativeBase):
    """全部 ORM 模型的基类。"""


engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    # uvicorn 是多线程处理请求, SQLite 默认禁止跨线程复用连接, 需显式放开;
    # timeout=30: 写锁竞争时驱动层最多排队 30s 再报 "database is locked"。
    connect_args=(
        {"check_same_thread": False, "timeout": 30}
        if settings.database_url.startswith("sqlite")
        else {}
    ),
)

if settings.database_url.startswith("sqlite"):
    # Phase 14 教训: 本库同时存在归档循环 / AI 调度器 / API 请求三类写者,
    # 默认 rollback-journal 下"长读事务会饿死写者"。WAL 让读写不互斥,
    # busy_timeout 兜底两个写者相撞 —— 每条新连接都施加 (WAL 是库级持久
    # 属性, 重复设置幂等; busy_timeout 是连接级, 必须每连接设)。
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_tune(dbapi_conn: object, _record: object) -> None:
        cursor = dbapi_conn.cursor  # type: ignore[attr-defined]
        cur = cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
        finally:
            cur.close()

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


def init_db() -> None:
    """启动时建表 (幂等, 已存在则跳过)。

    Phase 4 用 create_all 足够; 等表结构开始频繁演进再引入 Alembic 迁移。
    """
    from app.db import models  # noqa: F401  导入即注册到 Base.metadata

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    logger.info("数据库初始化完成: %s", settings.database_url.split("///")[-1])


def _ensure_columns() -> None:
    """幂等补列: create_all 只建缺失的表, 不会给已存在的表加新列。

    Phase 10 给 ai_analysis_records 加了 tool_trace, Phase 12 加了
    trigger_source —— 老库(卷已存在)必须手动 ALTER 才不会有
    "no such column" 报错。仅补有默认值的非破坏列。
    """
    from sqlalchemy import inspect

    insp = inspect(engine)
    if not insp.has_table("ai_analysis_records"):
        return
    existing = {c["name"] for c in insp.get_columns("ai_analysis_records")}
    if "tool_trace" not in existing:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE ai_analysis_records "
                    "ADD COLUMN tool_trace TEXT NOT NULL DEFAULT ''"
                )
            )
        logger.info("已补列: ai_analysis_records.tool_trace")
    if "trigger_source" not in existing:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE ai_analysis_records "
                    "ADD COLUMN trigger_source TEXT NOT NULL DEFAULT 'MANUAL'"
                )
            )
        logger.info("已补列: ai_analysis_records.trigger_source")


def db_healthcheck() -> bool:
    """真实执行 SELECT 1 验证数据库可用, 不做任何假返回。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("数据库健康检查失败")
        return False


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖: 每个请求一个 Session, 结束必定关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
