# syntax=docker/dockerfile:1
# Phase 1: 最小后端镜像
FROM python:3.11-slim

# 避免 pyc 缓存 & 输出即刷
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
# 阿里云 PyPI 镜像: VM 到官方源 DNS 抖动严重, 直连经常超时; 提高 retries/timeout 兜底
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --retries 10 --timeout 60 \
    -r requirements.txt

COPY app ./app

# 数据目录 (sqlite / 导出文件), 命名卷挂载点
RUN mkdir -p /app/data && \
    useradd --create-home --shell /usr/sbin/nologin zops && \
    chown -R zops:zops /app
USER zops

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
