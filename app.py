import hashlib
import csv
import io
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, TypedDict

import dashscope
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.graph import END, StateGraph
from openai import OpenAI
from pydantic import BaseModel, Field
from openpyxl import load_workbook

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
# pymilvus reads MILVUS_URI during import and only accepts a server URL there.
# Preserve the former project setting under a private name for Milvus Lite.
if os.getenv("MILVUS_URI") and not os.getenv("RAG_MILVUS_URI"):
    os.environ["RAG_MILVUS_URI"] = os.environ["MILVUS_URI"]
os.environ.pop("MILVUS_URI", None)

from pymilvus import MilvusClient

SOURCE_DIR = ROOT / "Documents"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
COLLECTION = "rag_chunks_v1"
SCRIPT_COLLECTION = "rag_scripts_v1"
SCRIPT_DB = DATA_DIR / "scripts.db"
DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
MIN_RETRIEVAL_SCORE = float(os.getenv("MIN_RETRIEVAL_SCORE", "0.25"))
EMBEDDING_BATCH_SIZE = 20
MAX_HISTORY_TURNS = 8
CONVERSATIONS: dict[str, list[dict[str, str]]] = {}
CONVERSATIONS_LOCK = Lock()
MILVUS_CLIENT: MilvusClient | None = None
MILVUS_LOCK = Lock()
app = FastAPI(title="RAG Explore")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class IngestRequest(BaseModel):
    strategy: str = Field(default="markdown_heading_v1", pattern="^(markdown_heading_v1|recursive_chars_v1)$")
    chunk_size: int = Field(default=480, ge=120, le=2000)
    overlap: int = Field(default=70, ge=0, le=400)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    rewrite: bool = True
    top_k: int = Field(default=4, ge=1, le=10)
    session_id: str | None = Field(default=None, max_length=64)


class ScriptPayload(BaseModel):
    intent: str = Field(default="通用", max_length=80)
    scene: str = Field(default="售前", max_length=80)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    status: str = Field(default="draft", pattern="^(draft|published|offline)$")
    operator: str = Field(default="运营", max_length=80)


class RAGState(TypedDict, total=False):
    question: str
    history: list[dict[str, str]]
    rewrite: bool
    top_k: int
    retrieval_query: str
    chunks: list[dict[str, Any]]
    answer: str
    citations: list[dict[str, Any]]
    warning: str
    grounded: bool


def trace_event(trace: list[dict[str, Any]], node: str, started: float, **details: Any) -> None:
    trace.append({"node": node, "duration_ms": round((time.perf_counter() - started) * 1000), "details": details})


def milvus() -> MilvusClient:
    global MILVUS_CLIENT
    with MILVUS_LOCK:
        if MILVUS_CLIENT is None:
            MILVUS_CLIENT = MilvusClient(uri=os.getenv("RAG_MILVUS_URI", str(DATA_DIR / "milvus.db")))
        return MILVUS_CLIENT


def script_milvus() -> MilvusClient:
    """Use a short-lived client for write-after-delete consistency in Milvus standalone."""
    return MilvusClient(uri=os.getenv("RAG_MILVUS_URI", str(DATA_DIR / "milvus.db")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(SCRIPT_DB)
    connection.row_factory = sqlite3.Row
    return connection


def init_script_db() -> None:
    with db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS scripts (
                id TEXT PRIMARY KEY,
                intent TEXT NOT NULL,
                scene TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft', 'published', 'offline')),
                version INTEGER NOT NULL DEFAULT 1,
                operator TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def row_to_script(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def get_script(script_id: str) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
    if not row:
        raise HTTPException(404, "话术不存在")
    return row_to_script(row)


def list_scripts(status: str | None = None) -> list[dict[str, Any]]:
    query, params = "SELECT * FROM scripts", []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY updated_at DESC"
    with db() as connection:
        rows = connection.execute(query, params).fetchall()
    return [row_to_script(row) for row in rows]


def script_text(script: dict[str, Any]) -> str:
    return f"话术标题：{script['title']}\n意图：{script['intent']}\n场景：{script['scene']}\n话术内容：{script['content']}"


init_script_db()


def split_recursive(text: str, size: int, overlap: int) -> list[tuple[str, str]]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    result, current = [], ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= size:
            current = (current + "\n\n" + paragraph).strip()
            continue
        if current:
            result.append((current, "全文"))
        while len(paragraph) > size:
            result.append((paragraph[:size], "全文"))
            paragraph = paragraph[max(1, size - overlap):]
        current = paragraph
    if current:
        result.append((current, "全文"))
    return result


def split_markdown(text: str, size: int, overlap: int) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    path: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            if current:
                sections.append(("\n".join(current).strip(), " / ".join(path) or "文档开头"))
                current = []
            level, title = len(match.group(1)), match.group(2).strip()
            path = path[: level - 1] + [title]
        current.append(line)
    if current:
        sections.append(("\n".join(current).strip(), " / ".join(path) or "文档开头"))
    result: list[tuple[str, str]] = []
    for body, heading in sections:
        if len(body) <= size:
            result.append((body, heading))
        else:
            result.extend((piece, heading) for piece, _ in split_recursive(body, size, overlap))
    return result


def chunks_for_file(path: Path, strategy: str, size: int, overlap: int) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    ingest_config = f"{strategy}:size={size}:overlap={overlap}"
    pieces = split_markdown(raw, size, overlap) if strategy == "markdown_heading_v1" else split_recursive(raw, size, overlap)
    document_id = str(path.relative_to(SOURCE_DIR).with_suffix(""))
    return [{"id": int(hashlib.sha256(f"{digest}:{strategy}:{size}:{overlap}:{index}".encode()).hexdigest()[:15], 16), "chunk_id": hashlib.sha256(f"{digest}:{strategy}:{size}:{overlap}:{index}".encode()).hexdigest()[:16], "document_id": document_id, "source_file": str(path.relative_to(ROOT)), "source_hash": digest, "heading_path": heading, "chunk_index": index, "text": text, "strategy": strategy, "ingest_config": ingest_config} for index, (text, heading) in enumerate(pieces)]


def embed(texts: list[str], text_type: str) -> list[list[float]]:
    key = os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")
    client = dashscope.Client(api_key=key, region="cn-beijing")
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        response = client.embeddings.create(
            model="qwen3.7-text-embedding",
            input=texts[start : start + EMBEDDING_BATCH_SIZE],
            dimension=DIMENSION,
            text_type=text_type,
        )
        payload = response.model_dump() if hasattr(response, "model_dump") else response
        items = payload.get("embeddings") or payload.get("output", {}).get("embeddings") or []
        if len(items) != len(texts[start : start + EMBEDDING_BATCH_SIZE]):
            raise RuntimeError("Embedding 返回条数与输入不一致")
        vectors.extend(item["embedding"] for item in items)
    return vectors


def ensure_collection(client: MilvusClient) -> None:
    if not client.has_collection(COLLECTION):
        client.create_collection(collection_name=COLLECTION, dimension=DIMENSION, primary_field_name="id", vector_field_name="vector", id_type="int", metric_type="COSINE", auto_id=False, enable_dynamic_field=True)


def ensure_script_collection(client: MilvusClient) -> None:
    if not client.has_collection(SCRIPT_COLLECTION):
        client.create_collection(collection_name=SCRIPT_COLLECTION, dimension=DIMENSION, primary_field_name="id", vector_field_name="vector", id_type="int", metric_type="COSINE", auto_id=False, enable_dynamic_field=True)


def script_filter(script_id: str) -> str:
    return f"script_id == {json.dumps(script_id)}"


def script_primary_id(script_id: str) -> int:
    return int(hashlib.sha256(f"script:{script_id}".encode()).hexdigest()[:15], 16)


def sync_script_vectors(scripts: list[dict[str, Any]]) -> None:
    client = script_milvus()
    unpublished = [script for script in scripts if script["status"] != "published"]
    if client.has_collection(SCRIPT_COLLECTION):
        for script in unpublished:
            client.delete(SCRIPT_COLLECTION, ids=[script_primary_id(script["id"])])
    published = [script for script in scripts if script["status"] == "published"]
    if published:
        ensure_script_collection(client)
        vectors = embed([script_text(script) for script in published], "document")
        rows = []
        for script, vector in zip(published, vectors):
            digest = hashlib.sha256(f"script:{script['id']}".encode()).hexdigest()
            rows.append({"id": script_primary_id(script["id"]), "chunk_id": digest[:16], "document_id": f"script:{script['id']}", "script_id": script["id"], "source_file": "话术管理后台", "heading_path": f"{script['scene']} / {script['intent']} / {script['title']}", "text": script_text(script), "status": script["status"], "version": script["version"], "updated_at": script["updated_at"], "record_type": "customer_script", "vector": vector})
        client.upsert(SCRIPT_COLLECTION, rows)
    if client.has_collection(SCRIPT_COLLECTION) and (unpublished or published):
        client.flush(SCRIPT_COLLECTION)
        client.load_collection(SCRIPT_COLLECTION)


def sync_script_vector(script: dict[str, Any]) -> None:
    sync_script_vectors([script])


def indexed_documents(client: MilvusClient) -> dict[str, set[tuple[str | None, str | None]]]:
    """Read per-document version metadata without loading vector fields."""
    if not client.has_collection(COLLECTION):
        return {}
    iterator = client.query_iterator(COLLECTION, batch_size=1000, output_fields=["document_id", "source_hash", "ingest_config"])
    documents: dict[str, set[tuple[str | None, str | None]]] = {}
    try:
        while rows := iterator.next():
            for row in rows:
                document_id = row.get("document_id")
                if document_id:
                    documents.setdefault(document_id, set()).add((row.get("source_hash"), row.get("ingest_config")))
    finally:
        iterator.close()
    return documents


def document_filter(document_id: str) -> str:
    return f"document_id == {json.dumps(document_id, ensure_ascii=False)}"


def collection_count(client: MilvusClient) -> int:
    """Return the logical row count, excluding deleted rows awaiting compaction."""
    if not client.has_collection(COLLECTION):
        return 0
    rows = client.query(COLLECTION, filter="id >= 0", output_fields=["count(*)"])
    return int(rows[0]["count(*)"]) if rows else 0


def history_as_text(history: list[dict[str, str]]) -> str:
    return "\n".join(f"{turn['role']}：{turn['content']}" for turn in history[-MAX_HISTORY_TURNS:])


def rewrite_query(question: str, history: list[dict[str, str]]) -> str:
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return question
    messages = [{"role": "system", "content": "把用户童装咨询改写成适合知识库检索的一行关键词。若当前问题包含“这个、那款、它、还有”等指代，可参考对话历史补全检索对象。不可回答问题、不可增加不存在的商品事实，只输出改写结果。"}]
    if history:
        messages.append({"role": "user", "content": f"对话历史（仅用于理解指代）：\n{history_as_text(history)}"})
    messages.append({"role": "user", "content": f"当前问题：{question}"})
    completion = OpenAI(api_key=key, base_url="https://api.deepseek.com").chat.completions.create(model="deepseek-flash", temperature=0, max_tokens=80, messages=messages)
    return completion.choices[0].message.content.strip() or question


def retrieve(query: str, limit: int) -> list[dict[str, Any]]:
    client = milvus()
    collections = [name for name in (COLLECTION, SCRIPT_COLLECTION) if client.has_collection(name)]
    if not collections:
        return []
    vector = embed([query], "query")[0]
    results: list[dict[str, Any]] = []
    for collection in collections:
        client.load_collection(collection)
        hits = client.search(collection, data=[vector], limit=limit, output_fields=["text", "chunk_id", "source_file", "heading_path", "strategy", "script_id", "version"])
        results.extend({"score": round(float(hit["distance"]), 4), **hit["entity"]} for hit in hits[0])
    return sorted(results, key=lambda item: item["score"], reverse=True)[:limit]


def grounded_answer(question: str, chunks: list[dict[str, Any]], history: list[dict[str, str]]) -> str:
    if not chunks:
        return "资料中没有足够信息回答这个问题。请补充商品尺码表、成分或售后政策，或转人工客服确认。"
    context = "\n\n".join(f"[{item['chunk_id']}] {item['text']}" for item in chunks)
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return "已检索到相关资料，但 DEEPSEEK_API_KEY 未配置，暂不能生成回答。"
    messages = [{"role": "system", "content": "你是童装客服助手。只可依据给出的资料回答；资料未说明时明确说不知道或建议人工确认。不可捏造库存、价格、成分、标准和售后政策。对话历史只能帮助理解指代，不能作为事实依据。回答简洁友好，末尾用“来源：[chunk_id]”列出支撑答案的编号。"}]
    if history:
        messages.append({"role": "user", "content": f"对话历史（仅用于理解上下文）：\n{history_as_text(history)}"})
    messages.append({"role": "user", "content": f"当前问题：{question}\n\n可引用资料：\n{context}"})
    completion = OpenAI(api_key=key, base_url="https://api.deepseek.com").chat.completions.create(model="deepseek-flash", temperature=0.2, max_tokens=500, messages=messages)
    return completion.choices[0].message.content.strip()


def extract_citations(answer: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only return source chunks explicitly cited by the model."""
    cited_ids = set(re.findall(r"\[([0-9a-f]{16})\]", answer))
    return [chunk for chunk in chunks if chunk["chunk_id"] in cited_ids]


def validate_grounded_answer(answer: str, chunks: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
    citations = extract_citations(answer, chunks)
    return bool(citations), citations


def build_graph(trace: list[dict[str, Any]]):
    def do_rewrite(state: RAGState) -> RAGState:
        started = time.perf_counter()
        query = rewrite_query(state["question"], state.get("history", [])) if state["rewrite"] else state["question"]
        trace_event(trace, "query_rewrite", started, original=state["question"], rewritten=query, history_turns=len(state.get("history", [])))
        return {"retrieval_query": query}
    def do_retrieve(state: RAGState) -> RAGState:
        started = time.perf_counter()
        chunks = retrieve(state["retrieval_query"], 10)
        trace_event(trace, "retrieve", started, candidates=[{"chunk_id": c["chunk_id"], "score": c["score"], "heading": c["heading_path"]} for c in chunks])
        selected = chunks[:state["top_k"]]
        return {"chunks": selected}
    def route(state: RAGState) -> str:
        chunks = state.get("chunks", [])
        if not chunks or chunks[0]["score"] < MIN_RETRIEVAL_SCORE:
            return "clarify"
        return "answer"
    def clarify(_: RAGState) -> RAGState:
        return {"answer": "资料中没有找到足够相关的内容。请提供商品名称、宝宝身高体重，或补充对应商品资料。", "citations": [], "warning": "低置信检索：已安全拒答"}
    def answer(state: RAGState) -> RAGState:
        started = time.perf_counter()
        text = grounded_answer(state["question"], state["chunks"], state.get("history", []))
        trace_event(trace, "generate", started, context_chunk_count=len(state["chunks"]), history_turns=len(state.get("history", [])))
        return {"answer": text}
    def groundedness_check(state: RAGState) -> RAGState:
        started = time.perf_counter()
        grounded, citations = validate_grounded_answer(state["answer"], state["chunks"])
        trace_event(trace, "groundedness_check", started, grounded=grounded, citation_ids=[c["chunk_id"] for c in citations])
        if grounded:
            return {"grounded": True, "citations": citations}
        return {
            "grounded": False,
            "answer": "我找到了相关资料，但无法生成可核验的引用回答。请重试，或转人工客服确认。",
            "citations": [],
            "warning": "引用校验未通过：已安全降级",
        }
    graph = StateGraph(RAGState)
    graph.add_node("query_rewrite", do_rewrite)
    graph.add_node("retrieve", do_retrieve)
    graph.add_node("clarify", clarify)
    graph.add_node("answer", answer)
    graph.add_node("groundedness_check", groundedness_check)
    graph.set_entry_point("query_rewrite")
    graph.add_edge("query_rewrite", "retrieve")
    graph.add_conditional_edges("retrieve", route, {"clarify": "clarify", "answer": "answer"})
    graph.add_edge("clarify", END)
    graph.add_edge("answer", "groundedness_check")
    graph.add_edge("groundedness_check", END)
    return graph.compile()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/customer")
def customer() -> FileResponse:
    return FileResponse(ROOT / "static" / "customer.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    client = milvus()
    count = collection_count(client)
    return {"collection": COLLECTION, "chunk_count": count, "source_files": [str(p.relative_to(SOURCE_DIR)) for p in SOURCE_DIR.rglob("*.md")]}


@app.post("/api/ingest")
def ingest(request: IngestRequest) -> dict[str, Any]:
    started = time.perf_counter()
    paths = sorted(SOURCE_DIR.rglob("*.md"))
    if not paths:
        raise HTTPException(400, "Documents/ 中没有 Markdown 文档")
    chunks_by_document = {str(path.relative_to(SOURCE_DIR).with_suffix("")): chunks_for_file(path, request.strategy, request.chunk_size, request.overlap) for path in paths}
    if not any(chunks_by_document.values()):
        raise HTTPException(400, "没有可入库的文本切片")
    client = milvus()
    ensure_collection(client)
    existing = indexed_documents(client)
    desired_versions = {document_id: (chunks[0]["source_hash"], chunks[0]["ingest_config"]) for document_id, chunks in chunks_by_document.items()}
    added = sorted(document_id for document_id in desired_versions if document_id not in existing)
    updated = sorted(document_id for document_id, version in desired_versions.items() if document_id in existing and existing[document_id] != {version})
    unchanged = sorted(document_id for document_id, version in desired_versions.items() if document_id in existing and existing[document_id] == {version})
    deleted = sorted(document_id for document_id in existing if document_id not in desired_versions)
    to_write = added + updated
    new_chunks = [chunk for document_id in to_write for chunk in chunks_by_document[document_id]]
    vectors = embed([chunk["text"] for chunk in new_chunks], "document") if new_chunks else []
    for document_id in updated + deleted:
        client.delete(COLLECTION, filter=document_filter(document_id))
    if new_chunks:
        client.insert(COLLECTION, [{**chunk, "vector": vector} for chunk, vector in zip(new_chunks, vectors)])
    if updated or deleted or new_chunks:
        # Standalone Milvus updates collection statistics after a flush. Without
        # it, search can return new rows while the UI still reports an old count.
        client.flush(COLLECTION)
    client.load_collection(COLLECTION)
    total_chunks = collection_count(client)
    return {"chunk_count": total_chunks, "preview": new_chunks[:8], "strategy": request.strategy, "duration_ms": round((time.perf_counter() - started) * 1000), "sync": {"added_documents": added, "updated_documents": updated, "deleted_documents": deleted, "unchanged_documents": unchanged, "embedded_chunks": len(new_chunks)}}


@app.get("/api/scripts")
def scripts(status: str | None = None) -> dict[str, Any]:
    if status and status not in {"draft", "published", "offline"}:
        raise HTTPException(400, "status 只能是 draft、published 或 offline")
    rows = list_scripts(status)
    return {"items": rows, "total": len(rows)}


@app.post("/api/scripts")
def create_script(payload: ScriptPayload) -> dict[str, Any]:
    script_id, timestamp = uuid.uuid4().hex, now_iso()
    script = {"id": script_id, **payload.model_dump(), "version": 1, "created_at": timestamp, "updated_at": timestamp}
    with db() as connection:
        connection.execute("INSERT INTO scripts (id, intent, scene, title, content, status, version, operator, created_at, updated_at) VALUES (:id, :intent, :scene, :title, :content, :status, :version, :operator, :created_at, :updated_at)", script)
    sync_script_vector(script)
    return script


@app.put("/api/scripts/{script_id}")
def update_script(script_id: str, payload: ScriptPayload) -> dict[str, Any]:
    current = get_script(script_id)
    script = {"id": script_id, **payload.model_dump(), "version": current["version"] + 1, "created_at": current["created_at"], "updated_at": now_iso()}
    with db() as connection:
        connection.execute("UPDATE scripts SET intent=:intent, scene=:scene, title=:title, content=:content, status=:status, version=:version, operator=:operator, updated_at=:updated_at WHERE id=:id", script)
    sync_script_vector(script)
    return script


@app.delete("/api/scripts/{script_id}")
def delete_script(script_id: str) -> dict[str, Any]:
    script = get_script(script_id)
    with db() as connection:
        connection.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
    script["status"] = "offline"
    sync_script_vector(script)
    return {"deleted": True, "id": script_id}


IMPORT_ALIASES = {"id": "id", "编号": "id", "意图": "intent", "intent": "intent", "场景": "scene", "适用场景": "scene", "scene": "scene", "标题": "title", "话术标题": "title", "话术名称": "title", "title": "title", "内容": "content", "话术内容": "content", "content": "content", "状态": "status", "status": "status", "操作人": "operator", "operator": "operator"}


def normalized_import_rows(filename: str, content: bytes) -> list[dict[str, str]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        source_rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    elif suffix in {".xlsx", ".xlsm"}:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        values = list(sheet.values)
        if not values:
            return []
        headers = [str(value).strip() if value is not None else "" for value in values[0]]
        source_rows = [dict(zip(headers, ["" if value is None else str(value).strip() for value in row])) for row in values[1:] if any(value is not None and str(value).strip() for value in row)]
    else:
        raise HTTPException(400, "仅支持 .csv、.xlsx 或 .xlsm 文件")
    rows = []
    for raw in source_rows:
        # Legacy customer-service exports commonly use: 序号、场景、适用场景、话术内容.
        # There, 场景 is the title while 适用场景 is the operational context.
        if {"序号", "场景", "适用场景", "话术内容"}.issubset(raw):
            sequence = str(raw.get("序号") or "").strip()
            stable_id = f"{Path(filename).stem}-{sequence}" if sequence else ""
            rows.append({"id": stable_id, "title": str(raw.get("场景") or "").strip(), "scene": str(raw.get("适用场景") or "").strip(), "content": str(raw.get("话术内容") or "").strip(), "intent": "售后"})
            continue
        row = {IMPORT_ALIASES.get(str(key).strip(), str(key).strip()): str(value).strip() for key, value in raw.items() if key}
        if row:
            rows.append(row)
    return rows


@app.post("/api/scripts/import")
async def import_scripts(file: UploadFile = File(...)) -> dict[str, Any]:
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "导入文件不能超过 5MB")
    rows = normalized_import_rows(file.filename or "", content)
    if not rows:
        raise HTTPException(400, "导入文件没有数据行")
    errors = []
    prepared = []
    for number, row in enumerate(rows, start=2):
        if not row.get("title") or not row.get("content"):
            errors.append(f"第 {number} 行缺少标题或内容")
            continue
        try:
            payload = ScriptPayload(intent=row.get("intent") or "通用", scene=row.get("scene") or "售前", title=row["title"], content=row["content"], status=row.get("status") or "draft", operator=row.get("operator") or "批量导入")
        except Exception:
            errors.append(f"第 {number} 行字段不合法（状态只能为 draft、published 或 offline）")
            continue
        prepared.append((row.get("id"), payload))
    if errors:
        raise HTTPException(400, {"message": "导入未执行", "errors": errors[:20]})
    changed, created, updated = [], 0, 0
    with db() as connection:
        for requested_id, payload in prepared:
            existing = connection.execute("SELECT * FROM scripts WHERE id = ?", (requested_id,)).fetchone() if requested_id else None
            timestamp = now_iso()
            if existing:
                script = {"id": requested_id, **payload.model_dump(), "version": existing["version"] + 1, "created_at": existing["created_at"], "updated_at": timestamp}
                connection.execute("UPDATE scripts SET intent=:intent, scene=:scene, title=:title, content=:content, status=:status, version=:version, operator=:operator, updated_at=:updated_at WHERE id=:id", script)
                updated += 1
            else:
                script = {"id": requested_id or uuid.uuid4().hex, **payload.model_dump(), "version": 1, "created_at": timestamp, "updated_at": timestamp}
                connection.execute("INSERT INTO scripts (id, intent, scene, title, content, status, version, operator, created_at, updated_at) VALUES (:id, :intent, :scene, :title, :content, :status, :version, :operator, :created_at, :updated_at)", script)
                created += 1
            changed.append(script)
    sync_script_vectors(changed)
    return {"created": created, "updated": updated, "published_synced": sum(script["status"] == "published" for script in changed), "items": changed}


@app.post("/api/ask")
def ask(request: AskRequest) -> dict[str, Any]:
    trace: list[dict[str, Any]] = []
    trace_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    session_id = request.session_id or uuid.uuid4().hex
    with CONVERSATIONS_LOCK:
        history = list(CONVERSATIONS.get(session_id, []))
    try:
        result = build_graph(trace).invoke({"question": request.question, "history": history, "rewrite": request.rewrite, "top_k": request.top_k})
    except Exception as exc:
        raise HTTPException(500, f"运行失败：{exc}") from exc
    with CONVERSATIONS_LOCK:
        updated_history = (history + [{"role": "用户", "content": request.question}, {"role": "助手", "content": result["answer"]}])[-MAX_HISTORY_TURNS:]
        CONVERSATIONS[session_id] = updated_history
    trace_event(trace, "complete", started, trace_id=trace_id)
    return {**result, "session_id": session_id, "history_turns": len(updated_history), "trace_id": trace_id, "trace": trace}


@app.delete("/api/conversations/{session_id}")
def clear_conversation(session_id: str) -> dict[str, bool]:
    with CONVERSATIONS_LOCK:
        CONVERSATIONS.pop(session_id, None)
    return {"cleared": True}
