# RAG Explore：童装客服知识库

本地运行的童装客服 RAG 学习项目。它将 Markdown 知识资料与运营话术分开管理，使用 Milvus 做语义检索，为管理端和消费者客服页提供带引用的回答。

## 当前定位

适用于单店或小团队的本地演示、内部试点和 RAG 技术学习；尚不适合作为直接面向消费者的正式生产系统。

## 已完成能力

### RAG 与知识库

- Markdown 标题切分、递归定长切分。
- Qwen embedding、Milvus COSINE dense 检索。
- 查询改写、LangGraph 路由、低置信度安全降级、引用校验与运行 Trace。
- 连续对话：内存中保留最近 4 轮上下文。
- `Documents/` 的文件级增量同步：新增、修改、删除、跳过未变文件，并显示调整日志。

### 话术运营

- SQLite `data/scripts.db` 为话术事实源。
- 单条新增、编辑、下线、删除；记录版本、操作人和更新时间。
- `draft` 草稿、`published` 已发布、`offline` 已下线三种状态。
- 仅 `published` 话术生成 embedding 并同步到 `rag_scripts_v1`。
- 支持 CSV、`.xlsx`、`.xlsm` 导入；已有稳定 `id` 的记录会更新并提升版本。
- 兼容旧售后话术表头：`序号、场景、适用场景、话术内容`。

### 页面与服务

- 管理端：<http://127.0.0.1:8000/>
- To C 客服页：<http://127.0.0.1:8000/customer>，默认主动问候并提供快捷问题。
- Docker 本地服务：Milvus `19530`、Attu <http://127.0.0.1:3000>。

## 架构

```text
运营后台 / CSV / Excel
        ↓
SQLite（scripts.db）
        ↓ 仅已发布话术
Embedding → Milvus（rag_scripts_v1）

Markdown → 增量同步 → Milvus（rag_chunks_v1）
                               ↓
管理端问答 / To C 客服页 → 检索、回答、来源引用
```

## 启动

### 1. 配置环境变量

复制 `.env.example` 为 `.env` 并填写模型服务 API Key。不要提交 `.env` 或在文档中记录密钥。

```dotenv
RAG_MILVUS_URI=http://127.0.0.1:19530
EMBEDDING_DIMENSION=1024
MIN_RETRIEVAL_SCORE=0.25
```

### 2. 启动 Milvus 与 Attu

```bash
docker-compose -f docker-compose.milvus.yml up -d
```

- Milvus：<http://127.0.0.1:19530>
- 健康检查：<http://127.0.0.1:9091/healthz>
- Attu：<http://127.0.0.1:3000>

### 3. 启动应用

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

访问管理端后，首次点击“增量同步知识库”将 `Documents/` 中的 Markdown 写入 Milvus。

## 标准话术导入格式

推荐的 CSV / Excel 表头：

```csv
id,title,content,intent,scene,status,operator,external_ref
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 否 | 稳定业务 ID；有则更新，无则新增 |
| `title` | 是 | 话术标题 |
| `content` | 是 | 客服回复正文 |
| `intent` | 否 | 例如退货、退款、尺码咨询 |
| `scene` | 否 | 触发或适用场景 |
| `status` | 否 | `draft`、`published`、`offline`；省略为 `draft` |
| `operator` | 否 | 维护人员 |
| `external_ref` | 否 | 外部工单、商品或规则编号 |

草稿不会参与检索。将状态设为“已发布”并保存后，系统会自动生成 embedding 并写入 Milvus。

## Milvus Collections

| Collection | 内容 | 用途 |
| --- | --- | --- |
| `rag_chunks_v1` | Markdown 切片、向量与来源 metadata | 文档知识库检索 |
| `rag_scripts_v1` | 已发布话术、版本与 metadata | 运营话术检索 |

在 Attu 的 Data / Query 页面可查看文本、`source_file`、`heading_path`、`document_id`、`chunk_id`、`script_id` 和 `version` 等 metadata。通常无须查看冗长的 `vector` 数组。

## API 概览

| 接口 | 用途 |
| --- | --- |
| `GET /api/status` | 知识库状态 |
| `POST /api/ingest` | 增量同步 Markdown |
| `POST /api/ask` | 检索并生成回答 |
| `GET/POST /api/scripts` | 查询或创建话术 |
| `PUT/DELETE /api/scripts/{id}` | 更新或删除话术 |
| `POST /api/scripts/import` | CSV / Excel 批量导入 |

## 未完成：生产化差距

### 高优先级

- 登录、角色权限、审核与批量发布。
- PostgreSQL / MySQL、备份、迁移与恢复策略。
- Redis / 数据库会话持久化、过期与隐私清理。
- 异步任务队列：embedding、导入、重试、进度和告警。
- 导入预览、错误行导出、批次审计和回滚。
- HTTPS、限流、鉴权、密钥托管与内容安全。

### 中优先级

- chunk 级增量、文档版本回滚。
- 定时发布、灰度发布、话术版本对比。
- 结构化日志、指标、链路追踪和告警面板。
- RAG 评测集、人工反馈闭环、召回与引用质量监控。
- 商品、订单、物流等业务系统实时接入。

### 当前限制

- 话术批量导入默认草稿，当前需要逐条编辑发布。
- Markdown 是文件级判断、文档级替换，尚非 chunk 级 diff。
- Milvus、SQLite 和应用为本地单机开发部署。
- 对话历史位于应用内存，重启后会清空。

## 项目文件

```text
app.py                       FastAPI、RAG 与话术 API
Documents/                   Markdown 知识资料
static/index.html            管理端
static/customer.html         To C 客服页
docker-compose.milvus.yml    Milvus、etcd、MinIO、Attu
logic_flow.md                RAG 流程图
pre_init.md                  初始方案与演进说明
```
