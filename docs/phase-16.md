# Phase 16: 平台完善 (大盘闭环可视化 + 端到端总闸)

## 目标

P1-15 把管道一节节接通; P16 让"整条管道"第一次在同一屏里可见、可巡、可反复体检:

1. 大盘 (app/static/index.html, 原生 JS 零构建) 新增闭环四面板:
   AI 分析归档 (SUCCESS/FAILED 如实 + AUTO/MANUAL + 是否检索过知识库)、
   审批单 (状态色 + 待批计数 chip + 审批人/结果)、审计哈希链 (链健康 chip +
   事件流)、知识库 (BM25 检索框 + 最近条目);
2. 问题归档每行"时间线"按钮 → /audit/timeline/problem/{id} 组装
   告警→分析→建议→批准/否决→留痕 全链证据 (缺的环节写"无, 不补假数据");
3. verify_phase16.sh 非破坏总闸: 不演练新能力, 只对全平台做只读巡检;
4. README.md 重写为平台终稿 (架构/流水线/16 Phase 表)。

## 关键决策

| 决策 | 理由 |
|---|---|
| 总闸全程非破坏 (零 stop/start, 零写调用) | 完善阶段的验证必须"随时可跑、跑多少遍都不改状态"—— 演示前、改代码后都能来一遍; 破坏性演练留在各阶段自己的 verify 脚本里 |
| [5/6] 用"伪造复盘探针"主动撞 422 | 红线不能只靠回忆: 故意 POST category=CLOSED_LOOP, 结构上被 schema 正则拒掉才算数 |
| 大盘不引任何前端框架/构建链 | 与后端"零未验证依赖"同一纪律; 原生 JS 单文件, scp 即部署 |
| 面板数据全部走既有只读 API | 大盘不新开旁路; 每张面板背后都是 [4/6] 里逐一点过 200 的端点 |
| 时间线用事件委托挂按钮 | 问题行随 10s 自动刷新重建, 逐行绑会丢; 委托一次挂稳 |
| XSS: 新面板统一 esc() 转义 | 分析正文/错误信息来自模型与外部系统, 不裸拼 innerHTML |
| 版本推进 0.16.0 会"废掉"旧阶段脚本的版本断言 | 各阶段 verify 脚本核对各自版本是既定口径; P16 起平台总状态以 verify_phase16.sh 为准, 历史脚本仅回放时用 |

## 大盘面板 ↔ 数据源对照

- AI 分析归档 → GET /api/v1/ai/analyses?limit=8 (含 FAILED)
- 审批单/待批 → GET /api/v1/exec/requests + GET /api/v1/exec/config (pending_requests)
- 审计链健康 → GET /api/v1/audit/verify (chip) + GET /api/v1/audit?limit=8
- 知识库检索/最近 → GET /api/v1/knowledge/search?q= + GET /api/v1/knowledge?limit=5
- 问题时间线 → GET /api/v1/audit/timeline/problem/{id}
- 既有面板不动: 主机 / Docker 汇总 (agent2 实采) / 问题归档 / 后端自检

## 验证 (scripts/verify_phase16.sh, 通关 PASS=6 FAIL=0, 约 1-3 分钟)

1. [1/6] 0.16.0 + 大盘五块闭环视图标记齐全 (HTML 逐标记对质);
2. [2/6] openapi 五域代表端点在册 (problems/ai/exec/audit/knowledge);
3. [3/6] 哈希链重放 ok + 0.16.0 的 system.startup 已入链;
4. [4/6] 10 个只读端点一跳全通 code=0;
5. [5/6] 四条安全红线不回归: 9 工具全只读含 kb_search / audit 面零写方法 /
   CLOSED_LOOP 手工伪造必 422 / exec approval_required 恒 true;
6. [6/6] LLM configured + AUTO 调度 ticks>=1 + 容器系 unsupported=0
   (镜像系噪音单列)。

## 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| [1/6] dash=missing:... | static/index.html 没 scp 全或没 --build | 重新 scp 后 `docker compose up -d --build backend`; 大盘是打进镜像的, 改前端也要重建 |
| [3/6] startup=无 或版本对不上 | 旧容器还在跑 / 重启前旧事件 | `docker logs zops-backend` 看是否已按 0.16.0 启动; system.startup 每条都带 version |
| [4/6] 某端点非 JSON | 反代/端口不对 | 确认 BACKEND_PORT; 直连 127.0.0.1 排除 nginx 因素 |
| [5/6] 探针 != 422 | 路由/schema 被动过 | 核对 app/schemas/knowledge.py 的 category 正则; 单测 test_knowledge_routes 也应红 |
| [6/6] tick=0 | AI_AUTO_ENABLED=false 或 LLM 未配 | 看 /api/v1/ai/auto 的 enabled/llm_configured; .env 改后重建 backend |
| 时间线面板 404 | 点了他库问题 id (换过库文件) | 时间线只组装真实归档; 换库后重新跑一轮演练闭环再来 |
