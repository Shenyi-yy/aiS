"""通用响应包装: 全部 /api/v1 接口统一 {code, message, data} 结构。"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """业务统一返回体。

    code=0 表示成功; 后续阶段错误码在此约定 (1xxx 配置 / 2xxx Zabbix / 3xxx LLM)。
    """

    code: int = 0
    message: str = "ok"
    data: T
