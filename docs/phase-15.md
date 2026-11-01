# Phase 15: RAG 知识库 (BM25 检索 + 闭环复盘 + Agent 注入)

## 目标

把"故障处置经验"变成可检索的资产, 并接进分析链路:

1. 知识库表与 API: knowledge_entries (RUNBOOK/CASE/DOC) + /api/v1/knowledge* 七个端点;
2. 复盘只从真实闭环生成: 归档问题 + SUCCESS 分析 + 执行单据 + 审计事件,
   缺环就拒绝 (不存在 404 / 没分析 409), 绝不生成想象中的复盘;
3. 检索双通道接进 Agent: 分析前 round=0 证据注入 (rag_retrieve 记入 tool_trace)
   + 第 9 个只读工具 kb_search —— AI 依然没有任何执行路径;
4. 知识库写操作 (created/updated/deleted) 全部入 P14 审计哈希链。

## 关键决策 (为什么这么做)

| 决策 | 理由 |
|---|---|
| BM25 (Okapi) 而非向量库 | VM→embedding 服务链路未验证, 拿没验证的依赖做核心链路=演示翻车风险; BM25 零外部依赖、离线可复现、得分可解释; 运维文档量级语料每次现算, 无缓存一致性坑 |
| 中文分词用 bigram + ASCII 词 | 不引分词库的确定性方案; "容器已停止"→容器/器已/已停/停止, 检索够用且可单测 |
| tags 在文档向量里重复一次 | 人贴的标签是显式相关性信号, 等价于字段加权 |
| 只回 score>0 的命中 | 检索诚实: 无关查询返回空, 不拿"最不离谱的"硬凑 |
| 复盘 CLOSED_LOOP 幂等 (每问题至多一条) | verify 重跑不产生重复语料, 不刷审计; created=false 就是幂等证据 |
| 注入通道恒记 round=0 轨迹 | "查没查知识库、命中几条"必须事后可审 (tool_trace + 审计 detail.tools_used) |
| 零命中不注入参考 system 消息 | 没东西可给时不给, 模型不能感觉到"知识库假装在场" |
| kb_search 是第 9 个只读工具 | 白名单 8→9 仍是全 GET 语义; 写路径只在 API 路由, 模型侧结构上不存在 |
| 写操作入链, detail 带 before/after | 知识库自己也要能被"改了什么、谁改的"复盘; 删除事件先入链再删行 |

## 接口一览 (/api/v1 前缀)

- POST /knowledge 手工录入 (category 只允许 RUNBOOK/DOC —— schema 正则收口)
- GET /knowledge 分页; GET /knowledge/search?q=&top_k= (top_k 上限 5)
- GET|PUT|DELETE /knowledge/{entry_id}
- POST /knowledge/case/{problem_id} 闭环复盘生成 (幂等)

坑 (已在单测里钉死): /search 与 /case 必须声明在 /{entry_id} 之前,
否则 "search" 被当 entry_id → 422。

## 验证 (scripts/verify_phase15.sh, 通关 PASS=6 FAIL=0)

1. [1/6] 0.15.0 + 四件套入镜像 + /knowledge 路由注册 (openapi 对质);
2. [2/6] 红线复核: /ai/tools 恰 9 个全只读含 kb_search, /audit 面仍零写方法;
3. [3/6] 复盘诚实闸门: case/999999→404; 取真实 EXECUTED 关联问题生成复盘
   (content 内嵌真实分析), 再调一次 created=false 同 id (幂等);
4. [4/6] BM25 行为: "zops-drill 容器 停止"命中且 score>0, 乱码查询 count=0;
5. [5/6] 真实注入: 基线 id 后 stop drill → 新 ACTIVE 归档 → AUTO 分析
   tool_trace 必含 round=0 rag_retrieve 且 hits>=1 → 否决 AUTO 建议单
   (actor=verify15) → 拉起 drill —— 检索在用, 执行仍只走人;
6. [6/6] 链收口: /audit/verify ok + knowledge.created>=1 + execution.rejected>=1
   + 容器系 unsupported=0 (镜像系噪音单列)。

前置: P13/P14 的 verify 跑过 (库里有"分析成功且批准执行过"的真实闭环,
[3/6] 的语料从它生成)。篡改检测不在真库演示, 由单测覆盖 (同 P14 口径)。

## 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| [1/6] routes=no | 只 scp 没 --build / /search 路由顺序错 | 确认 up -d --build backend; 单测 test_knowledge_routes 也护着这条 |
| [3/6] EXECUTED 关联问题=无 | 库里从未批准执行过任何关联问题的单据 | 先跑一轮 P13/P14 演练闭环再来 |
| [3/6] bad:code=409 | 选中的问题没有 SUCCESS 分析 | 诚实闸门按设计工作; 等 AUTO 分析完成或手工分析后重跑 |
| [5/6] hits=0 | 语料与告警无关 (复盘没生成/标签不沾边) | 先过 [3/6][4/6]; 或手工 POST 一条 RUNBOOK (标题含主机/触发器关键词) |
| [5/6] 600s 等不到 AUTO 分析 | LLM Key/调度关了/老问题挡道 (防陈旧已滤) | 看 /api/v1/ai/auto 与 backend 日志 |
| 检索永远空但表里有数据 | 查询词太"整句" —— bigram 分词按词命中 | 用关键词组合 ("zops-drill 容器 停止"), 别贴整段日志 |
