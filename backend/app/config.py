"""后端配置管理 (Phase 1 最小版)

所有敏感配置来自环境变量 / .env, 禁止硬编码。
pydantic-settings 读取优先级: 环境变量 > .env 文件 > 默认值。
后续阶段在此追加 Zabbix / LLM 配置字段, 业务代码不改。
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置, 字段名与 .env 中的变量名一一对应 (大小写不敏感)。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env 中还有 Zabbix/LLM 等其他变量, Phase 1 忽略
    )

    # ---------- 应用基础 ----------
    app_name: str = Field(default="zops-backend", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ---------- 数据库 (Phase 1 仅声明, 暂不使用) ----------
    database_url: str = Field(default="sqlite:///./data/app.db", alias="DATABASE_URL")

    # ---------- Zabbix (Phase 5 接入 API) ----------
    zabbix_url: str = Field(default="http://zabbix-web:8080", alias="ZABBIX_URL")
    zabbix_api_url: str = Field(
        default="http://zabbix-web:8080/api_jsonrpc.php", alias="ZABBIX_API_URL"
    )
    # 凭据只存在于 Ubuntu 侧 /opt/zops/.env → compose 注入容器环境变量;
    # 代码与仓库中永远不出现真实密码。
    zabbix_username: str = Field(default="", alias="ZABBIX_USERNAME")
    zabbix_password: str = Field(default="", alias="ZABBIX_PASSWORD")
    # 归档同步: 周期(秒, 最小 15) 与回溯天数 (Zabbix events 保留期内)
    zabbix_sync_interval: int = Field(default=60, alias="ZABBIX_SYNC_INTERVAL")
    zabbix_sync_days: int = Field(default=30, alias="ZABBIX_SYNC_DAYS")

    # ---------- Phase 9: LLM (OpenAI 兼容协议, 供应商可换) ----------
    # 三要素全在 Ubuntu 侧 /opt/zops/.env: 换供应商只改这三个值, 代码不动。
    # 例: LLM_BASE_URL=https://api.deepseek.com/v1  LLM_MODEL=deepseek-chat
    # Key 绝不进代码 / Git / 聊天记录 / 容器镜像层。
    llm_base_url: str = Field(default="", alias="LLM_BASE_URL")
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_model: str = Field(default="", alias="LLM_MODEL")
    # 大模型响应慢是常态 (深度思考模型可达 30s+), 默认给 120s
    llm_timeout: int = Field(default=120, alias="LLM_TIMEOUT")
    # Phase 10: Agent 工具循环最大轮数 (防失控; 超限抛 LLMError 并归档 FAILED)
    llm_agent_max_rounds: int = Field(default=6, alias="LLM_AGENT_MAX_ROUNDS", ge=1, le=20)

    # ---------- Phase 12: AI 自动故障分析调度 ----------
    # 后台循环: ACTIVE 且尚无分析记录的归档 → 自动跑带工具取证的分析。
    # 关掉开关 (AI_AUTO_ENABLED=false) 后一切照旧, 只是不再自动烧 token。
    ai_auto_enabled: bool = Field(default=True, alias="AI_AUTO_ENABLED")
    ai_auto_interval: int = Field(
        default=60, alias="AI_AUTO_INTERVAL", ge=30, le=3600,
        description="扫描周期秒 (下限 30: 分析本身就要几十秒, 再快无意义)",
    )
    ai_auto_max_per_tick: int = Field(
        default=3, alias="AI_AUTO_MAX_PER_TICK", ge=1, le=10,
        description="单轮最多自动分析几条, 防止告警风暴把额度烧穿",
    )

    # ---------- Phase 13: 人工确认 + 安全执行 ----------
    # 执行通道 = 挂进容器的 docker.sock (Engine API, 无 shell);
    # 目标容器白名单在这里, 动作白名单在 exec_catalog.ALLOWED_ACTIONS 代码里。
    # EXEC_ENABLED=false 时批准接口返回 503, 任何执行都不会发生。
    exec_enabled: bool = Field(default=True, alias="EXEC_ENABLED")
    exec_auto_suggest: bool = Field(
        default=True, alias="EXEC_AUTO_SUGGEST",
        description="自动分析成功后是否按目录匹配生成待审批建议 (仍须人批准)",
    )
    exec_allow_targets: str = Field(
        default="zops-drill", alias="EXEC_ALLOW_TARGETS",
        description="逗号分隔容器名白名单; 留空 = 关闭一切目标 (目录全不可见)",
    )
    exec_docker_socket: str = Field(
        default="/var/run/docker.sock", alias="EXEC_DOCKER_SOCKET"
    )
    exec_timeout: int = Field(
        default=30, alias="EXEC_TIMEOUT", ge=5, le=300,
        description="Docker Engine API 单次往返超时 (秒)",
    )

    # ---------- Phase 14: 审计日志 ----------
    # 哈希链只追加证据流。AUDIT_ENABLED=false 只关"写入", 查询/校验接口
    # 照常可用 —— 关审计不等于毁审计。
    audit_enabled: bool = Field(default=True, alias="AUDIT_ENABLED")


@lru_cache
def get_settings() -> Settings:
    """全局单例配置, 避免重复解析环境变量。"""
    return Settings()
