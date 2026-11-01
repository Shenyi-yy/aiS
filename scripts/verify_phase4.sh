#!/usr/bin/env bash
# =============================================================
# Phase 4 验证脚本: FastAPI 业务后端骨架
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase4.sh
# 通关标准: PASS=6 FAIL=0
# 判读提示: 每项自己打印 [PASS]/[FAIL]; 最后一行打印结果汇总。
#           把完整输出贴回来即可, 不需要额外解释。
# =============================================================
set -u

BASE="http://127.0.0.1:${BACKEND_PORT:-8000}"
PASS=0
FAIL=0

ok() { echo "  [PASS] $1"; PASS=$((PASS+1)); }
ng() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

# 从 stdin 的 JSON 里取值的公共 python 片段 (失败打印空串)
jget() {
  python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        d=d[int(k)] if k.isdigit() else d[k]
    print(d)
except Exception:
    print("")' "$1"
}

echo "== [1/6] backend 容器运行中 =="
STATE=$(docker inspect -f '{{.State.Status}}' zops-backend 2>/dev/null || echo missing)
if [ "${STATE}" = "running" ]; then
  ok "zops-backend = running"
else
  ng "zops-backend = ${STATE} (期望 running; 先执行: cd /opt/zops && docker compose up -d --build backend)"
fi

echo "== [2/6] GET /health 存活检查 (Phase 1 形状未变) =="
S=$(curl -sf --max-time 5 "${BASE}/health" | jget "status")
if [ "${S}" = "ok" ]; then
  ok "/health status=ok"
else
  ng "/health 无响应或格式不符 (got='${S}'); 查: docker logs zops-backend --tail 30"
fi

echo "== [3/6] GET /api/v1/system/info 真实数据库自检 =="
DB_OK=$(curl -sf --max-time 5 "${BASE}/api/v1/system/info" | jget "data.db_connected")
if [ "${DB_OK}" = "True" ]; then
  ok "db_connected=True (容器内 SELECT 1 实测通过)"
else
  ng "db_connected=${DB_OK} (期望 True); 查: docker logs zops-backend --tail 30"
fi

echo "== [4/6] GET /api/v1/problems 归档查询接口 =="
PROB_JSON=$(curl -sf --max-time 5 "${BASE}/api/v1/problems")
TOTAL=$(echo "${PROB_JSON}" | jget "data.total")
ISLIST=$(echo "${PROB_JSON}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    print(isinstance(d["data"]["items"], list))
except Exception:
    print("False")')
if [ -n "${TOTAL}" ] && [ "${ISLIST}" = "True" ]; then
  ok "problems 接口结构正确 (total=${TOTAL}; Phase 5 前为 0 条属正常, 有数据才是造假)"
else
  ng "problems 接口异常 (total='${TOTAL}' items是列表=${ISLIST})"
fi

echo "== [5/6] problem_records 表真实建出 =="
PY_CHECK="from sqlalchemy import inspect
from app.db.base import engine
print('yes' if 'problem_records' in inspect(engine).get_table_names() else 'no')"
HAS_TABLE=$(docker exec zops-backend python -c "${PY_CHECK}" 2>/dev/null || echo err)
if [ "${HAS_TABLE}" = "yes" ]; then
  ok "problem_records 表存在于容器 SQLite (/app/data/app.db)"
else
  ng "查表结果=${HAS_TABLE} (no=启动未建表, err=导入失败); 查: docker exec zops-backend python -c 'import app.main'"
fi

echo "== [6/6] OpenAPI 路由注册完整 =="
PATHS=$(curl -sf --max-time 5 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    p=json.load(sys.stdin)["paths"]
    need=["/health","/api/v1/system/info","/api/v1/problems"]
    print("all" if all(n in p for n in need) else "missing:" + ",".join(n for n in need if n not in p))
except Exception:
    print("err")')
if [ "${PATHS}" = "all" ]; then
  ok "/health + /api/v1/system/info + /api/v1/problems 均已注册"
else
  ng "openapi 路由不全 (${PATHS}) — 多半跑的还是旧镜像, 需要 --build 重建"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 4 验证通过, 可以进入 Phase 5"
else
  echo "Phase 4 未通过: 按上面 [FAIL] 行的提示排查后重跑本脚本"
fi
