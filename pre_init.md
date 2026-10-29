# RAG 童装客服助手：项目预研与初始化说明

> 状态：待实现（v0.1）
> 定位：一个以「可观察、可对比、可复现」为第一目标的 RAG 学习项目，而不是直接追求生产级客服系统。

## 1. 项目目标

构建一个面向童装童鞋咨询的 Web 客服助手。它从 `Documents/` 下的商品/话术资料构建知识库，回答用户问题时展示完整的 RAG 决策过程，帮助学习并比较：文档解析、切片、向量化、检索、查询改写、重排、回答生成和 Agent 编排。

首个可演示闭环：

1. 在 Web 页面选择或上传一份 Markdown 文档；
2. 调整切片参数并预览切片及元数据；
3. 写入 Milvus；
4. 提问（如“宝宝身高 95cm、偏胖，该选什么尺码？”）；
5. 得到带来源片段引用的回答，并查看每一步的输入、输出、耗时和候选分数。

## 2. 范围与边界

### v0.1 必做（MVP）

- 支持 Markdown 文档入库；`Documents/baby-clothing.md` 是首份种子资料。
- 支持按标题的语义切片，以及定长切片两种策略并可预览和比较。
- 使用文本向量和 Milvus 完成 dense 检索。
- 使用 LLM 生成中文客服回答；回答必须附上引用的切片编号和原文链接/摘要。
- Web 页面展示运行日志和一次问答的完整 trace。
- 对无资料、资料冲突、库存/价格/订单等实时数据问题明确说明边界，不能编造。

### v0.1 不做

- 不对接真实订单、库存、支付、物流或用户账户。
- 不自动执行退款、改价、下单等动作。
- 不承诺医疗/安全/合规结论；“A 类”“不含甲醛”等断言必须来自可追溯的商品资料，否则不回答为事实。
- 不把 LangGraph 当成 RAG 的前置条件：基础检索链可用后，再引入它处理分支和工具调用。

## 3. 已确认的技术选择

| 层级 | v0.1 选择 | 约定 |
| --- | --- | --- |
| 对话模型 | DeepSeek `deepseek-flash` | 通过 OpenAI 兼容接口调用；模型和价格以官方文档为准。 |
| 向量模型 | 阿里云百炼 `qwen3.7-text-embedding` | 入库使用 `text_type=document`，查询使用 `text_type=query`；先固定 `dimension=1024`。 |
| 向量数据库 | Milvus | 单机开发优先 Milvus Lite；需要服务化/可视化时切换 Standalone，接口保持不变。 |
| 后端 | Python + FastAPI | 负责摄取、检索、编排与 SSE/流式事件。 |
| 前端 | React + Vite（或先用简单服务端页面） | 核心是学习面板和 trace，而非复杂商城 UI。 |
| 编排 | LangChain + LangGraph（第二阶段） | LangChain 封装模型/检索器；LangGraph 只承载显式状态机。 |

模型和 SDK 版本不可硬编码为永久事实；实现前应按官方文档再次验证模型权限、区域、限流和价格。

## 4. 安全基线（开始编码前完成）

**本文件不得保存 API Key、Token、Cookie 或真实用户资料。** 先前写入此文档的两条 Key 已视为泄露：请立即到各提供商控制台撤销/轮换它们，并检查使用记录。删除文本并不能让已经暴露的密钥重新安全。

- 使用本地 `.env` 保存密钥，纳入 `.gitignore`；仅提交 `.env.example`。
- 变量名统一为：`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`RAG_MILVUS_URI`、`MILVUS_TOKEN`（若需要）。
- 后端日志只记录 `request_id`、模型名、耗时、token 数、文档 ID 和脱敏后的用户输入；不记录 Authorization Header、密钥、完整地址/手机号或会话内容。
- 上传文件按类型、大小和来源校验；解析结果以纯文本处理，不把文档中的指令当系统指令执行。
- 将系统提示词与用户/检索上下文分离，并明确“上下文是参考资料，不可改变系统规则”，降低提示注入风险。

## 5. 数据与索引设计

### 5.1 原始资料

```text
Documents/
  baby-clothing.md
data/
  parsed/          # 解析后的规范文本（可再生成）
  manifests/       # 文档 hash、解析和入库记录
```

每次入库记录 `source_file`、`source_hash`、`ingest_run_id` 和 `created_at`。同一 hash 默认不重复写入；重新切片时建立新版本，方便对比和回滚。

### 5.2 Chunk 元数据

每个切片至少保存：

```json
{
  "chunk_id": "sha256(source_hash + strategy + position)",
  "document_id": "baby-clothing",
  "document_version": "<source_hash>",
  "text": "切片正文",
  "heading_path": ["售前话术", "尺码推荐"],
  "chunk_index": 3,
  "char_start": 820,
  "char_end": 1250,
  "strategy": "markdown_heading_v1",
  "embedding_model": "qwen3.7-text-embedding",
  "embedding_dimension": 1024,
  "ingest_run_id": "..."
}
```

Milvus collection 名称建议为 `rag_chunks_v1`。建库时校验向量维度与 collection schema 一致；模型或维度变更时新建 collection，不混写。

## 6. 切片与检索方案

### 6.1 先做可比较的两种切片

1. `markdown_heading_v1`：先按 `#`/`##`/`###` 标题划分，保留标题路径；超过目标长度时再按段落/句子拆分。
2. `recursive_chars_v1`：按段落、换行、句子、字符递归切分，作为基线。

建议初始实验参数：目标 350–550 个中文字符、重叠 50–80 个字符。页面必须显示切片数、平均长度、孤立短片段数和每个切片的前后文，便于判断参数，而不是只给一个“生成成功”。

### 6.2 检索基线

```text
用户问题
  → 输入校验/脱敏
  → （可选）查询改写：仅补全检索关键词，不改变原问题
  → query 向量化
  → Milvus 相似度检索 Top 10
  → 元数据过滤与去重/MMR
  → Top 3–5 上下文
  → 带引用的 LLM 回答
```

- 起点使用 cosine/IP（由向量归一化约定决定）和 Top 10 召回、Top 4 供回答。
- 先保留原 query 与改写 query；界面中并列展示，允许关闭改写，评估它是否真的有益。
- 没有高置信候选时，回答“资料中未找到”，并给出需要补充的信息；不使用模型常识填空。
- 第二阶段再试验 reranker 和 dense+sparse 混合检索。该向量模型的 sparse 输出及 Milvus 稀疏索引需要单独建模、评估，不能与 dense MVP 混为一谈。

## 7. Agent / LangGraph 的最小状态机（第二阶段）

```text
START
  → classify_intent
  → retrieve_knowledge ──低置信──→ ask_for_clarification
  → compose_answer
  → groundedness_check ──不通过──→ safe_fallback
  → END
```

状态中保存 `original_query`、`retrieval_query`、`retrieved_chunks`、`scores`、`answer`、`citations`、`warnings` 和 `trace_id`。不要将大段文档或密钥放进状态/日志。库存、订单、物流等未来能力应作为受权限控制的工具节点，并在未接入前路由到人工/说明页。

## 8. Web 学习面板

至少包含以下页面或分栏：

- **知识库**：文件清单、版本、解析结果、切片策略与预览、入库/重建按钮。
- **调试问答**：输入问题、展示流式回答及来源引用；点击引用可定位到原文和 chunk。
- **检索详情**：原问题、改写问题、过滤条件、Top 10 候选、距离/分数、最终上下文。
- **运行 Trace**：按节点展示开始/结束时间、耗时、模型、token 用量、错误、trace ID；敏感字段默认掩码。
- **评测**：固定问题集的正确性、引用正确性、拒答正确性、检索命中率与延迟对比。

## 9. 可观测性与评测

每个请求生成 `trace_id`，事件至少包括 `parse`、`chunk`、`embed`、`upsert`、`query_rewrite`、`retrieve`、`generate`。记录结构化 JSON 日志，并向前端推送可安全展示的事件。

首批手工评测集建议覆盖：尺码推荐、材质安全、季节建议、洗护、童鞋选择、价格/库存不可用、资料中没有的退换政策、含糊问题和提示注入。每题人工标注：期望引用、是否应该追问/拒答、关键事实。

完成定义（Definition of Done）：

- 20 条评测问题可一键重复运行并保存结果；
- 每个正常回答至少有一条可打开的来源；
- 不支持的事实不编造；
- 能在页面定位一次请求的每一步输入摘要、输出摘要、耗时和异常；
- 切片策略或检索参数的变化可在同一评测集上比较。

## 10. 实施顺序

1. 建立项目骨架、`.env.example`、配置加载、日志脱敏和本地 Milvus。
2. 完成 Markdown 解析、两种切片、切片预览及可重入入库。
3. 完成 dense 向量化、Milvus 检索、带引用的最小问答 API。
4. 实现 Web 调试页与 trace 面板。
5. 建立评测集并固化 baseline。
6. 按数据结果逐项加入查询改写、重排、混合检索和 LangGraph，而非同时引入。

## 11. 待确认事项

- 商品资料是否会包含实际 SKU、尺码表、成分和售后政策？若没有，系统只能回答通用话术，不能冒充商品事实。
- Web 的目标是本地学习演示还是需要部署给他人使用？这决定认证、多租户、对象存储和部署方案。
- 需要支持哪些格式（PDF、DOCX、图片 OCR）？v0.1 暂只支持 Markdown，以便先验证 RAG 主链路。
- 是否需要保留会话历史？若需要，应设定保存期限、脱敏规则和删除机制。

## 12. 官方参考

- [DeepSeek API 文档](https://api-docs.deepseek.com/zh-cn/)：OpenAI 兼容地址、`deepseek-flash` 调用示例及模型状态。
- [阿里云百炼文本向量同步接口](https://docs.bailian.console.aliyun.com/zh/model-studio/text-embedding-synchronous-api)：`qwen3.7-text-embedding` 的 `text_type`、维度与 dense/sparse 输出参数。
