import asyncio
import hashlib
import html
import logging
import re
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root and backend dir are in sys.path
_CURRENT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _CURRENT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(_CURRENT_DIR))

import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from database.db_manager import db_manager
from database.document_integrity import (
    CHUNK_SIZE,
    build_metadata,
    get_local_document,
    record_local_document,
    secure_filename,
    verify_local_document,
)
from database.firebase_service import firebase_service
from taskfinisher_orchestrator import TaskFinisherOrchestrator
from backend.app.db.database import init_db
from backend.app.api.documents import router as documents_router
from backend.app.api.retrieval import router as retrieval_router
from backend.app.api.research import router as research_router

logger = logging.getLogger("taskfinisher.api")

app = FastAPI(
    title="TASKFINISHER AI",
    description="An AI Agent That Actually Finishes the Job - TP01 Research Platform",
    version="1.1.0",
)

# Initialize TP01 Research Database
try:
    init_db()
except Exception as e:
    logger.warning("Could not initialize TP01 DB: %s", e)

# Mount TP01 Document, Retrieval & Research Engine routes
app.include_router(documents_router, prefix="/api")
app.include_router(retrieval_router, prefix="/api")
app.include_router(research_router, prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = TaskFinisherOrchestrator()
running_tasks: Dict[str, asyncio.Task] = {}

_BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = _BASE_DIR / "uploads"
OUTPUT_DIR = _BASE_DIR / "outputs"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".csv", ".docx", ".md", ".xlsx", ".xls", ".json"}
TERMINAL_STATUSES = {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "WAITING_FOR_HUMAN"}


class TaskRequest(BaseModel):
    user_request: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    output_format: str = "docx"
    scenario: Optional[str] = "success"
    uploaded_files: List[str] = Field(default_factory=list)


class HumanDecisionRequest(BaseModel):
    selected_option: Optional[str] = None
    decision: Optional[str] = None


class EngineConfiguration(BaseModel):
    model: str = "Gemini 2.0 Flash"
    verification_floor: int = Field(default=90, ge=70, le=98)
    max_retries: int = Field(default=2, ge=1, le=5)
    auto_recovery: bool = True
    cryptographic_seal: bool = True
    output_format: str = "docx"


class TaskResponse(BaseModel):
    task_id: str
    user_request: Optional[str] = None
    verdict: str
    status: str
    completed_steps: int
    total_steps: int
    completion_percentage: float
    evidence_trail: list
    requires_human_approval: bool
    human_decision_point: Optional[str] = None
    human_options: list = Field(default_factory=list)
    steps: Dict[str, Any] = Field(default_factory=dict)
    events: list = Field(default_factory=list)
    evidence: list = Field(default_factory=list)
    adk_trace: list = Field(default_factory=list)
    final_report: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _api_error(code: str, message: str, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": {"code": code, "message": message}})


def _task_text(task_request: TaskRequest) -> str:
    text = task_request.user_request or task_request.description or task_request.title or ""
    return text.strip()


def _created_task_payload(task_id: str, task_request: TaskRequest) -> Dict[str, Any]:
    text = _task_text(task_request)
    timestamp = _now()
    return {
        "task_id": task_id,
        "user_request": text,
        "title": task_request.title or text[:80],
        "description": task_request.description or text,
        "output_format": task_request.output_format,
        "scenario": task_request.scenario or "success",
        "status": "CREATED",
        "verdict": "PENDING",
        "completed_steps": 0,
        "total_steps": 0,
        "completion_percentage": 0,
        "evidence_trail": [],
        "evidence": [],
        "steps": {},
        "events": [{
            "event": "TASK_CREATED",
            "type": "task_created",
            "task_id": task_id,
            "message": "Task created and awaiting execution.",
            "timestamp": timestamp,
        }],
        "artifacts": {},
        "final_report": "",
        "adk_trace": [],
        "requires_human_approval": False,
        "human_decision_point": None,
        "human_options": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(event)
    event_name = normalized.get("event") or normalized.get("action") or normalized.get("type") or "event"
    normalized["event"] = event_name
    normalized["type"] = str(event_name).lower()
    normalized.setdefault("timestamp", _now())
    return normalized


def _request_user_id(authorization: Optional[str] = None) -> str:
    return firebase_service.user_id_from_request(authorization)


def _get_task_or_404(task_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    task = orchestrator.get_task(task_id)
    if not task and firebase_service.enabled:
        task = firebase_service.get_task(task_id, user_id)
    if not task:
        raise _api_error("TASK_NOT_FOUND", "Task not found.", 404)
    return task


def _local_task_history(
    limit: int = 25,
    search: Optional[str] = None,
    status: Optional[str] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    tasks = db_manager.get_all_tasks()
    if status:
        tasks = [task for task in tasks if task.get("status") == status]
    if search:
        needle = search.lower()
        tasks = [
            task for task in tasks
            if needle in str(task.get("user_request", "")).lower()
            or needle in str(task.get("title", "")).lower()
            or needle in str(task.get("status", "")).lower()
        ]
    if cursor:
        try:
            start = next(index + 1 for index, task in enumerate(tasks) if task.get("task_id") == cursor)
            tasks = tasks[start:]
        except StopIteration:
            tasks = []
    page = tasks[: max(1, min(limit, 100))]
    next_cursor = page[-1]["task_id"] if len(page) == limit and len(tasks) > limit else None
    return {"tasks": page, "next_cursor": next_cursor}


def _format_duration(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    minutes, remaining = divmod(seconds, 60)
    return f"{minutes}m {remaining:02d}s" if minutes else f"{remaining}s"


def _document_path_for_task(task: Dict[str, Any]) -> Optional[Path]:
    task_id = task.get("task_id")
    artifact_value = (
        task.get("artifacts", {}).get("preferred_report")
        or task.get("artifacts", {}).get("report.docx")
        or task.get("artifacts", {}).get("report.md")
    )
    if not artifact_value:
        for candidate in (OUTPUT_DIR / f"{task_id}_report.docx", OUTPUT_DIR / f"{task_id}_report.md"):
            candidate_path = (Path.cwd() / candidate).resolve()
            if candidate_path.exists():
                artifact_path = candidate_path
                break
        else:
            return None
    else:
        artifact_path = Path(artifact_value)
    if not artifact_path.is_absolute():
        artifact_path = Path.cwd() / artifact_path
    allowed_roots = {OUTPUT_DIR.resolve(), (Path.cwd() / "outputs").resolve(), (Path.cwd() / "backend" / "outputs").resolve()}
    resolved_artifact = artifact_path.resolve()
    if not any(root in resolved_artifact.parents or resolved_artifact == root for root in allowed_roots):
        raise _api_error("INVALID_DOCUMENT_PATH", "Invalid report artifact path.", 400)
    return resolved_artifact


def _validate_document(path: Path, task: Dict[str, Any]) -> Dict[str, Any]:
    report_text = (task.get("final_report") or "").lower()
    placeholder_markers = ("todo", "tbd", "lorem ipsum", "[insert", "{{", "}}")
    checks = {
        "exists": path.exists(),
        "non_empty": path.exists() and path.stat().st_size > 0,
        "openable": False,
        "no_placeholder_text": not any(marker in report_text for marker in placeholder_markers),
        "references_present": bool(task.get("evidence_trail") or task.get("evidence")),
    }
    if path.suffix.lower() == ".docx" and path.exists():
        try:
            with zipfile.ZipFile(path) as docx:
                checks["openable"] = "[Content_Types].xml" in docx.namelist() and "word/document.xml" in docx.namelist()
        except zipfile.BadZipFile:
            checks["openable"] = False
    elif path.exists():
        checks["openable"] = True
    return {"status": "ready" if all(checks.values()) else "failed", "checks": checks}


def _ensure_local_document_metadata(
    *,
    document_id: str,
    path: Path,
    user_id: str,
    task_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    content_type: Optional[str] = None,
) -> Dict[str, Any]:
    existing = get_local_document(document_id)
    metadata = build_metadata(
        document_id=document_id,
        path=path,
        user_id=user_id,
        task_id=task_id,
        execution_id=execution_id,
        content_type=content_type,
        version=int(existing.get("version") or 1) if existing else 1,
    )
    return record_local_document(metadata)


def _document_metadata_for_path(
    *,
    document_id: str,
    path: Path,
    user_id: str,
    task_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    content_type: Optional[str] = None,
) -> Dict[str, Any]:
    metadata = _ensure_local_document_metadata(
        document_id=document_id,
        path=path,
        user_id=user_id,
        task_id=task_id,
        execution_id=execution_id,
        content_type=content_type,
    )
    if firebase_service.enabled:
        firebase_service.save_uploaded_document(metadata, path)
    return metadata


def _report_text_for_task(task_id: str, task: Dict[str, Any]) -> str:
    report = orchestrator.get_report(task_id).get("report") or task.get("final_report") or ""
    if report:
        return report
    markdown_path = (Path.cwd() / OUTPUT_DIR / f"{task_id}_report.md").resolve()
    if markdown_path.exists():
        return markdown_path.read_text(encoding="utf-8")
    return ""


def _section_text(report: str, heading: str) -> str:
    pattern = rf"(?ms)^{re.escape(heading)}:\s*\n(.*?)(?=\n[A-Z][A-Za-z /-]+:\n|\Z)"
    match = re.search(pattern, report)
    return match.group(1).strip() if match else ""


def _important_points_markdown(task_id: str, task: Dict[str, Any], report: str) -> str:
    sections = {
        "Executive Summary": _section_text(report, "Executive Summary"),
        "Main Research Findings": _section_text(report, "Main Research Findings"),
        "Important Statistics / Data": _section_text(report, "Important Statistics / Data"),
        "Cross-Source Verification": _section_text(report, "Cross-Source Verification"),
        "Key Insights": _section_text(report, "Key Insights"),
        "Recommendations": _section_text(report, "Recommendations"),
        "References / Sources": _section_text(report, "References / Sources"),
    }
    lines = [
        "# Important Points Research Report",
        "",
        f"Task ID: {task_id}",
        f"Task: {task.get('user_request') or task.get('description') or 'Research task'}",
        f"Status: {task.get('verdict') or task.get('status') or 'UNKNOWN'}",
        f"Generated: {_now()} UTC",
        "",
    ]
    for title, content in sections.items():
        if content:
            lines.extend([f"## {title}", "", content, ""])
    if len(lines) <= 7:
        lines.extend([
            "## Important Points",
            "",
            report.strip() or "No final research report text was available for this task.",
            "",
        ])
    return "\n".join(lines).strip() + "\n"


def _docx_paragraph(text: str, style: Optional[str] = None, bullet: bool = False) -> str:
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    prefix = "- " if bullet else ""
    return (
        "<w:p>"
        f"<w:pPr>{style_xml}</w:pPr>"
        "<w:r>"
        f'<w:t xml:space="preserve">{html.escape(prefix + text)}</w:t>'
        "</w:r>"
        "</w:p>"
    )


def _write_docx_from_markdown(markdown: str, path: Path) -> None:
    body = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            body.append(_docx_paragraph(""))
        elif stripped.startswith("# "):
            body.append(_docx_paragraph(stripped[2:], "Title"))
        elif stripped.startswith("## "):
            body.append(_docx_paragraph(stripped[3:], "Heading1"))
        elif stripped.startswith("- "):
            body.append(_docx_paragraph(stripped[2:], bullet=True))
        else:
            body.append(_docx_paragraph(stripped))
    body.append(
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
        'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>'
        "</w:sectPr>"
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}</w:body></w:document>"
    )
    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:sz w:val="22"/></w:rPr></w:style>
</w:styles>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types_xml)
        docx.writestr("_rels/.rels", rels_xml)
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/styles.xml", styles_xml)


def _write_pdf_from_markdown(markdown_text: str, output_path: Path, title: str = "Research Intelligence Report") -> None:
    """Renders markdown text into an elegant, publication-grade PDF using PyMuPDF."""
    import pymupdf
    doc = pymupdf.open()
    page_w, page_h = 595.3, 841.9  # A4
    margin_l = 54.0
    margin_r = 54.0
    margin_t = 60.0
    margin_b = 60.0
    usable_w = page_w - margin_l - margin_r
    max_y = page_h - margin_b

    current_page = doc.new_page(width=page_w, height=page_h)
    y = margin_t

    def _check_page(needed_height: float):
        nonlocal current_page, y
        if y + needed_height > max_y:
            current_page = doc.new_page(width=page_w, height=page_h)
            y = margin_t

    lines = markdown_text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            y += 8
            continue

        if stripped.startswith("# "):
            _check_page(42)
            header_text = stripped[2:].strip()
            current_page.insert_text((margin_l, y + 18), header_text, fontsize=16, fontname="helv", color=(0.1, 0.1, 0.15))
            current_page.draw_line(
                pymupdf.Point(margin_l, y + 26),
                pymupdf.Point(margin_l + usable_w, y + 26),
                color=(1.0, 0.41, 0.0),  # #FF6A00
                width=1.5
            )
            y += 38
        elif stripped.startswith("## "):
            _check_page(30)
            header_text = stripped[3:].strip()
            current_page.insert_text((margin_l, y + 14), header_text, fontsize=12, fontname="helv", color=(0.15, 0.2, 0.25))
            current_page.draw_line(
                pymupdf.Point(margin_l, y + 19),
                pymupdf.Point(margin_l + usable_w, y + 19),
                color=(0.85, 0.85, 0.85),
                width=0.5
            )
            y += 28
        elif stripped.startswith("### "):
            _check_page(24)
            header_text = stripped[4:].strip()
            current_page.insert_text((margin_l, y + 11), header_text, fontsize=10.5, fontname="helv", color=(0.2, 0.25, 0.3))
            y += 20
        elif stripped.startswith("- ") or stripped.startswith("* "):
            bullet_text = stripped[2:].strip()
            words = bullet_text.split()
            cur_line = ""
            _check_page(16)
            current_page.insert_text((margin_l + 4, y + 10), "\u2022", fontsize=10, fontname="helv", color=(1.0, 0.41, 0.0))
            for word in words:
                test_line = f"{cur_line} {word}".strip()
                if len(test_line) * 5.2 > usable_w - 20:
                    current_page.insert_text((margin_l + 16, y + 10), cur_line, fontsize=9.5, fontname="helv", color=(0.15, 0.15, 0.15))
                    y += 13
                    _check_page(14)
                    cur_line = word
                else:
                    cur_line = test_line
            if cur_line:
                current_page.insert_text((margin_l + 16, y + 10), cur_line, fontsize=9.5, fontname="helv", color=(0.15, 0.15, 0.15))
                y += 14
        else:
            words = stripped.split()
            cur_line = ""
            for word in words:
                test_line = f"{cur_line} {word}".strip()
                if len(test_line) * 5.2 > usable_w:
                    _check_page(14)
                    current_page.insert_text((margin_l, y + 10), cur_line, fontsize=9.5, fontname="helv", color=(0.15, 0.15, 0.15))
                    y += 13
                    cur_line = word
                else:
                    cur_line = test_line
            if cur_line:
                _check_page(14)
                current_page.insert_text((margin_l, y + 10), cur_line, fontsize=9.5, fontname="helv", color=(0.15, 0.15, 0.15))
                y += 15

    total_pages = len(doc)
    for p_idx, page in enumerate(doc):
        page.insert_text(
            (margin_l, 36),
            "TP01 AI RESEARCH PLATFORM — MISSION DIRECTOR: SAMEER",
            fontsize=7.5,
            fontname="helv",
            color=(0.5, 0.5, 0.5)
        )
        page.draw_line(
            pymupdf.Point(margin_l, 42),
            pymupdf.Point(margin_l + usable_w, 42),
            color=(0.85, 0.85, 0.85),
            width=0.5
        )
        footer_text = f"Page {p_idx + 1} of {total_pages} | Verified Grounding & Cryptographic Audit"
        page.insert_text(
            (margin_l, page_h - 32),
            footer_text,
            fontsize=7.5,
            fontname="helv",
            color=(0.5, 0.5, 0.5)
        )
        page.draw_line(
            pymupdf.Point(margin_l, page_h - 40),
            pymupdf.Point(margin_l + usable_w, page_h - 40),
            color=(0.85, 0.85, 0.85),
            width=0.5
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()


def _important_points_artifact(task_id: str, task: Dict[str, Any]) -> Path:
    report = _report_text_for_task(task_id, task)
    if not report:
        raise _api_error("REPORT_NOT_READY", "Run research first; no completed report is available yet.", 404)
    OUTPUT_DIR.mkdir(exist_ok=True)
    markdown = _important_points_markdown(task_id, task, report)
    markdown_path = (Path.cwd() / OUTPUT_DIR / f"{task_id}_important_points.md").resolve()
    docx_path = (Path.cwd() / OUTPUT_DIR / f"{task_id}_important_points.docx").resolve()
    markdown_path.write_text(markdown, encoding="utf-8")
    _write_docx_from_markdown(markdown, docx_path)
    return docx_path


def _default_engine_config() -> Dict[str, Any]:
    return {
        "model": "Gemini 2.0 Flash",
        "model_label": "Gemini 2.0 Flash",
        "verification_floor": 90,
        "max_retries": 2,
        "auto_recovery": True,
        "cryptographic_seal": True,
        "output_format": "docx",
    }


def _normalize_engine_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = _default_engine_config()
    if config:
        normalized.update({k: v for k, v in config.items() if v is not None})
        if config.get("model") and not config.get("model_label"):
            normalized["model_label"] = config["model"]
    normalized["model_label"] = normalized.get("model_label") or normalized.get("model") or "Gemini 2.0 Flash"
    normalized["model"] = normalized.get("model_label")
    normalized["verification_floor"] = max(70, min(int(normalized.get("verification_floor", 90)), 98))
    normalized["max_retries"] = max(1, min(int(normalized.get("max_retries", 2)), 5))
    normalized["auto_recovery"] = bool(normalized.get("auto_recovery", True))
    normalized["cryptographic_seal"] = bool(normalized.get("cryptographic_seal", True))
    output_format = str(normalized.get("output_format") or "docx").lower()
    normalized["output_format"] = "md" if output_format in {"md", "markdown"} else "docx"
    return normalized


def _load_engine_config(user_id: str) -> Dict[str, Any]:
    saved = firebase_service.get_setting("engine_configuration", user_id) if firebase_service.enabled else None
    if not saved:
        saved = db_manager.get_setting("engine_configuration")
    config = _normalize_engine_config(saved)
    applied = orchestrator.apply_engine_config(config)
    return {**config, **applied, "model": config["model_label"], "model_label": config["model_label"]}


def _save_engine_config(config: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    normalized = _normalize_engine_config(config)
    applied = orchestrator.apply_engine_config(normalized)
    stored = {
        **normalized,
        **applied,
        "model": normalized["model_label"],
        "model_label": normalized["model_label"],
        "saved_at": _now(),
    }
    db_manager.save_setting("engine_configuration", stored)
    firebase_service.save_setting("engine_configuration", stored, user_id)
    return stored


async def _run_existing_agent(task_id: str, task_request: TaskRequest) -> None:
    try:
        logger.info("task execution started", extra={"task_id": task_id})
        result = await orchestrator.execute_task(
            _task_text(task_request),
            scenario=task_request.scenario or "success",
            uploaded_files=task_request.uploaded_files,
            task_id=task_id,
        )
        logger.info("task execution completed", extra={"task_id": task_id, "status": result.get("status")})
    except Exception:
        logger.exception("task execution failed", extra={"task_id": task_id})
        failed = _created_task_payload(task_id, task_request)
        failed.update({
            "status": "FAILED",
            "verdict": "FAILED",
            "events": failed["events"] + [{
                "event": "TASK_FAILED",
                "type": "task_failed",
                "task_id": task_id,
                "message": "The existing ADK agent execution failed.",
                "timestamp": _now(),
            }],
        })
        db_manager.save_task(failed)
    finally:
        running_tasks.pop(task_id, None)


@app.get("/")
async def root():
    return {
        "message": "TASKFINISHER AI - An AI Agent That Actually Finishes the Job",
        "version": "1.1.0",
        "adk_agents": list(orchestrator.agents.keys()),
        "tools": list(orchestrator.tools.keys()),
        "flow": "PLAN -> EXECUTE -> VERIFY -> RECOVER -> HUMAN DECISION -> FINAL VERIFY -> REPORT",
    }


@app.get("/health")
async def health_check():
    stats = db_manager.get_database_stats()
    adk_available = bool(orchestrator.agents)
    database_connected = bool(stats)
    return {
        "status": "ok" if adk_available and database_connected else "degraded",
        "adk": "available" if adk_available else "unavailable",
        "database": "connected" if database_connected else "unavailable",
        "firebase": "connected" if firebase_service.enabled else "not_configured",
    }


@app.get("/api/settings/engine")
async def get_engine_settings(authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    return _load_engine_config(user_id)


@app.post("/api/settings/engine")
async def save_engine_settings(settings: EngineConfiguration, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    config = _save_engine_config(settings.model_dump(), user_id)
    return {
        "status": "saved",
        "settings": config,
    }


@app.post("/execute", response_model=TaskResponse)
async def execute_task_legacy(task_request: TaskRequest):
    if not _task_text(task_request):
        raise _api_error("VALIDATION_ERROR", "A user_request or description is required.")
    return await orchestrator.execute_task(
        _task_text(task_request),
        scenario=task_request.scenario or "success",
        uploaded_files=task_request.uploaded_files,
    )


@app.post("/api/tasks")
async def create_task(task_request: TaskRequest):
    if not _task_text(task_request):
        raise _api_error("VALIDATION_ERROR", "A title, description, or user_request is required.")
    if task_request.user_request:
        return await execute_task_legacy(task_request)

    task_id = f"task_{uuid.uuid4().hex[:10]}"
    payload = _created_task_payload(task_id, task_request)
    db_manager.save_task(payload)
    logger.info("task created", extra={"task_id": task_id})
    return {"task_id": task_id, "status": "created"}


@app.get("/api/tasks")
async def list_tasks(
    authorization: Optional[str] = Header(None),
    limit: int = Query(25, ge=1, le=100),
    cursor: Optional[str] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
    task_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    user_id = _request_user_id(authorization)
    if firebase_service.enabled:
        result = firebase_service.list_tasks(
            user_id,
            limit=limit,
            cursor=cursor,
            search=search,
            status=status,
            task_type=task_type,
            date_from=date_from,
            date_to=date_to,
        )
    else:
        result = _local_task_history(limit=limit, search=search, status=status, cursor=cursor)

    tasks = []
    for task in result.get("tasks", []):
        duration = None
        if task.get("started_at") and task.get("completed_at"):
            try:
                started = datetime.fromisoformat(str(task.get("started_at")).replace("Z", ""))
                completed = datetime.fromisoformat(str(task.get("completed_at")).replace("Z", ""))
                duration = _format_duration((completed - started).total_seconds())
            except (TypeError, ValueError):
                duration = None
        tasks.append({
            **task,
            "duration": duration,
        })
    return {"tasks": tasks, "next_cursor": result.get("next_cursor")}


@app.post("/api/tasks/{task_id}/start")
async def start_task(task_id: str, authorization: Optional[str] = Header(None)):
    task = _get_task_or_404(task_id, _request_user_id(authorization))
    if task.get("status") in TERMINAL_STATUSES:
        return {"task_id": task_id, "execution_id": f"exec_{task_id}", "status": task.get("status", "completed").lower()}
    if task_id in running_tasks:
        return {"task_id": task_id, "execution_id": f"exec_{task_id}", "status": "executing"}

    task_request = TaskRequest(
        description=task.get("description") or task.get("user_request"),
        title=task.get("title"),
        output_format=task.get("output_format", "docx"),
        scenario=task.get("scenario", "success"),
        uploaded_files=task.get("uploaded_files", []),
    )
    running_tasks[task_id] = asyncio.create_task(_run_existing_agent(task_id, task_request))
    return {"task_id": task_id, "execution_id": f"exec_{task_id}", "status": "executing"}


@app.websocket("/api/tasks/{task_id}/stream")
async def stream_task_events(websocket: WebSocket, task_id: str):
    await websocket.accept()
    sent = 0
    try:
        while True:
            task = orchestrator.get_task(task_id) or db_manager.get_task(task_id)
            if not task:
                await websocket.send_json(_normalize_event({"event": "error", "task_id": task_id, "message": "Task not found."}))
                return

            events = task.get("events") or db_manager.get_execution_history(task_id)
            for event in events[sent:]:
                await websocket.send_json(_normalize_event(event))
            sent = len(events)

            if task.get("status") in TERMINAL_STATUSES and task_id not in running_tasks:
                return
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return


@app.post("/api/demo/{scenario}", response_model=TaskResponse)
async def run_demo(scenario: str):
    try:
        return await orchestrator.run_demo(scenario)
    except Exception:
        logger.exception("demo execution failed", extra={"scenario": scenario})
        raise _api_error("TASK_EXECUTION_FAILED", "The demo task could not be executed.", 500)


@app.get("/api/tasks/history")
async def get_task_history(authorization: Optional[str] = Header(None), limit: int = Query(25, ge=1, le=100)):
    result = await list_tasks(authorization=authorization, limit=limit)
    return {"history": result["tasks"], "tasks": result["tasks"], "next_cursor": result.get("next_cursor")}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    task = _get_task_or_404(task_id, user_id)
    if firebase_service.enabled:
        executions = firebase_service.list_subcollection(task_id, "executions", user_id)
        documents = firebase_service.list_subcollection(task_id, "documents", user_id)
        task = {
            **task,
            "execution_summary": executions[-1] if executions else None,
            "documents": documents,
        }
    return task


@app.get("/api/tasks/{task_id}/steps")
async def get_steps(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    _get_task_or_404(task_id, user_id)
    if firebase_service.enabled:
        return {
            item["step_id"]: item
            for item in firebase_service.list_subcollection(task_id, "steps", user_id)
        }
    return orchestrator.get_steps(task_id)


@app.get("/api/tasks/{task_id}/evidence")
async def get_evidence(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    _get_task_or_404(task_id, user_id)
    if firebase_service.enabled:
        return {"task_id": task_id, "evidence": firebase_service.list_subcollection(task_id, "evidence", user_id)}
    return {"task_id": task_id, "evidence": orchestrator.get_evidence(task_id)}


@app.get("/api/tasks/{task_id}/events")
async def get_events(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    _get_task_or_404(task_id, user_id)
    if firebase_service.enabled:
        return {
            "task_id": task_id,
            "events": [_normalize_event(event) for event in firebase_service.list_subcollection(task_id, "events", user_id)],
        }
    return {"task_id": task_id, "events": [_normalize_event(event) for event in orchestrator.get_events(task_id)]}


@app.get("/api/tasks/{task_id}/execution")
async def get_execution_replay(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    _get_task_or_404(task_id, user_id)
    if firebase_service.enabled:
        return [_normalize_event(event) for event in firebase_service.list_subcollection(task_id, "events", user_id)]
    return [_normalize_event(event) for event in orchestrator.get_events(task_id)]


@app.get("/api/tasks/{task_id}/executions")
async def get_task_executions(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    task = _get_task_or_404(task_id, user_id)
    if firebase_service.enabled:
        return {"task_id": task_id, "executions": firebase_service.list_subcollection(task_id, "executions", user_id)}

    started_at = task.get("created_at")
    completed_at = task.get("updated_at") if task.get("status") in TERMINAL_STATUSES else None
    return {
        "task_id": task_id,
        "executions": [{
            "execution_id": f"exec_{task_id}",
            "task_id": task_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": task.get("status"),
            "total_steps": task.get("total_steps", 0),
            "completed_steps": task.get("completed_steps", 0),
            "failed_steps": sum(1 for step in (task.get("steps") or {}).values() if step.get("status") == "FAILED"),
        }],
    }


@app.get("/api/tasks/{task_id}/executions/{execution_id}")
async def get_task_execution_details(task_id: str, execution_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    task = _get_task_or_404(task_id, user_id)
    if firebase_service.enabled:
        details = firebase_service.get_execution_details(task_id, execution_id, user_id)
        if details:
            return details
        raise _api_error("EXECUTION_NOT_FOUND", "Execution not found.", 404)

    events = [_normalize_event(event) for event in orchestrator.get_events(task_id)]
    steps = list((task.get("steps") or {}).values())
    return {
        "task": task,
        "execution": {
            "execution_id": execution_id,
            "task_id": task_id,
            "status": task.get("status"),
            "started_at": task.get("created_at"),
            "completed_at": task.get("updated_at") if task.get("status") in TERMINAL_STATUSES else None,
        },
        "steps": steps,
        "events": events,
        "agents": [event for event in events if event.get("event") == "ADK_AGENT_USED"],
        "tools": [event for event in events if "TOOL" in str(event.get("event", "")).upper()],
        "verification": [event for event in events if "VERIFICATION" in str(event.get("event", "")).upper()],
        "recovery": [event for event in events if "RECOVERY" in str(event.get("event", "")).upper() or "RETRY" in str(event.get("event", "")).upper()],
        "evidence": orchestrator.get_evidence(task_id),
        "human_actions": [],
        "documents": [],
        "final_result": {"status": task.get("status"), "verdict": task.get("verdict")},
    }


@app.get("/api/tasks/{task_id}/human-action")
async def get_human_action(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    task = _get_task_or_404(task_id, user_id)
    if firebase_service.enabled:
        actions = firebase_service.list_subcollection(task_id, "human_actions", user_id)
        if actions:
            return {"task_id": task_id, "actions": actions, **actions[-1]}
    return {
        "task_id": task_id,
        "status": "WAITING_FOR_HUMAN" if task.get("requires_human_approval") else "NONE",
        "question": task.get("human_decision_point"),
        "context": task.get("human_decision_point"),
        "options": task.get("human_options", []),
        "evidence": task.get("evidence", []),
        "timestamp": task.get("updated_at"),
    }


@app.post("/api/tasks/{task_id}/human-action", response_model=TaskResponse)
async def submit_human_action(task_id: str, decision: HumanDecisionRequest):
    return await submit_human_decision(task_id, decision)


@app.get("/api/tasks/{task_id}/report")
async def get_report(task_id: str, authorization: Optional[str] = Header(None)):
    _get_task_or_404(task_id, _request_user_id(authorization))
    return orchestrator.get_report(task_id)


@app.get("/api/tasks/{task_id}/report/download")
async def download_report(
    task_id: str,
    format: Optional[str] = Query("pdf", description="Format to download: 'pdf', 'docx', or 'md'"),
    authorization: Optional[str] = Header(None),
):
    user_id = _request_user_id(authorization)
    task = _get_task_or_404(task_id, user_id)
    report_text = orchestrator.get_report(task_id).get("report", "") or task.get("final_report", "")
    if not report_text:
        raise _api_error("DOCUMENT_NOT_FOUND", "Report content not found.", 404)

    fmt = (format or "pdf").lower().strip()
    OUTPUT_DIR.mkdir(exist_ok=True)

    if fmt == "docx":
        docx_path = (Path.cwd() / OUTPUT_DIR / f"{task_id}_report.docx").resolve()
        if not docx_path.exists():
            _write_docx_from_markdown(report_text, docx_path)
        metadata = _document_metadata_for_path(
            document_id=f"doc_{task_id}_docx",
            path=docx_path,
            user_id=user_id,
            task_id=task_id,
            execution_id=f"exec_{task_id}",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        return FileResponse(
            path=docx_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=f"{task_id}_report.docx",
        )

    elif fmt in {"md", "markdown"}:
        md_path = (Path.cwd() / OUTPUT_DIR / f"{task_id}_report.md").resolve()
        if not md_path.exists():
            md_path.write_text(report_text, encoding="utf-8")
        metadata = _document_metadata_for_path(
            document_id=f"doc_{task_id}_md",
            path=md_path,
            user_id=user_id,
            task_id=task_id,
            execution_id=f"exec_{task_id}",
            content_type="text/markdown; charset=utf-8",
        )
        return FileResponse(
            path=md_path,
            media_type="text/markdown; charset=utf-8",
            filename=f"{task_id}_report.md",
        )

    else:
        # Default: PDF format
        pdf_path = (Path.cwd() / OUTPUT_DIR / f"{task_id}_report.pdf").resolve()
        if not pdf_path.exists():
            _write_pdf_from_markdown(report_text, pdf_path, title=f"Research Report: {task_id}")
        metadata = _document_metadata_for_path(
            document_id=f"doc_{task_id}_pdf",
            path=pdf_path,
            user_id=user_id,
            task_id=task_id,
            execution_id=f"exec_{task_id}",
            content_type="application/pdf",
        )
        return FileResponse(
            path=pdf_path,
            media_type="application/pdf",
            filename=f"{task_id}_report.pdf",
        )


@app.get("/api/tasks/{task_id}/important-points/download")
async def download_important_points(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    task = _get_task_or_404(task_id, user_id)
    artifact_path = _important_points_artifact(task_id, task)
    metadata = _document_metadata_for_path(
        document_id=f"doc_{task_id}_important_points",
        path=artifact_path,
        user_id=user_id,
        task_id=task_id,
        execution_id=f"exec_{task_id}",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    verification = verify_local_document(metadata["document_id"])
    if verification["integrity"] != "VERIFIED":
        raise _api_error("FILE_INTEGRITY_ERROR", "Important-points document integrity verification failed before download.", 409)
    return FileResponse(
        path=artifact_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=artifact_path.name,
    )


@app.get("/api/documents/{document_id}")
async def get_document_metadata(document_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    if firebase_service.enabled:
        metadata = firebase_service.find_document(document_id, user_id)
        if metadata:
            return metadata
    task_id = document_id[4:] if document_id.startswith("doc_") else document_id
    task = _get_task_or_404(task_id, user_id)
    artifact_path = _document_path_for_task(task)
    if not artifact_path:
        raise _api_error("DOCUMENT_NOT_FOUND", "No document is available for this task.", 404)
    validation = _validate_document(artifact_path, task)
    metadata = _document_metadata_for_path(
        document_id=f"doc_{task_id}",
        path=artifact_path,
        user_id=user_id,
        task_id=task_id,
        execution_id=f"exec_{task_id}",
    )
    return {
        "document_id": metadata["document_id"],
        "task_id": task_id,
        "filename": artifact_path.name,
        "format": artifact_path.suffix.lstrip(".").lower(),
        "status": validation["status"],
        "created_at": task.get("updated_at") or task.get("created_at"),
        "size_bytes": artifact_path.stat().st_size if artifact_path.exists() else 0,
        "algorithm": "SHA-256",
        "sha256": metadata["sha256"],
        "integrity": metadata["integrity"],
        "version": metadata["version"],
        "validation": validation["checks"],
    }


@app.get("/api/documents/{document_id}/download")
async def download_document(document_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    if firebase_service.enabled:
        metadata = firebase_service.find_document(document_id, user_id)
        if metadata:
            verification = firebase_service.verify_document_integrity(document_id, user_id)
            if not verification or verification["integrity"] != "VERIFIED":
                raise _api_error("FILE_INTEGRITY_ERROR", "Document integrity verification failed before download.", 409)
            signed_url = firebase_service.signed_document_url(metadata["task_id"], document_id, user_id)
            if signed_url:
                return RedirectResponse(signed_url)
    local_verification = verify_local_document(document_id)
    if local_verification["integrity"] == "INTEGRITY_FAILED":
        raise _api_error("FILE_INTEGRITY_ERROR", "Document integrity verification failed before download.", 409)
    task_id = document_id[4:] if document_id.startswith("doc_") else document_id
    return await download_report(task_id, authorization)


@app.get("/api/documents/{document_id}/verify")
async def verify_document(document_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    if firebase_service.enabled:
        verification = firebase_service.verify_document_integrity(document_id, user_id)
        if verification:
            return verification
    return verify_local_document(document_id)


@app.get("/api/documents/{document_id}/integrity")
async def document_integrity(document_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    metadata = firebase_service.find_document(document_id, user_id) if firebase_service.enabled else None
    if not metadata:
        metadata = get_local_document(document_id)
    if not metadata:
        raise _api_error("DOCUMENT_NOT_FOUND", "Document integrity metadata was not found.", 404)
    verification = await verify_document(document_id, authorization)
    return {
        "document_id": document_id,
        "filename": metadata.get("filename"),
        "algorithm": "SHA-256",
        "sha256": metadata.get("sha256"),
        "file_size": metadata.get("size_bytes") or metadata.get("file_size"),
        "created_at": metadata.get("created_at"),
        "status": verification.get("integrity"),
        "version": metadata.get("version"),
    }


@app.get("/api/tasks/{task_id}/artifact")
async def get_artifact(task_id: str):
    report = orchestrator.get_report(task_id)
    return {
        "task_id": task_id,
        "file_name": f"{task_id}_report.md",
        "artifact_type": "markdown",
        "content": report.get("report", ""),
    }


@app.post("/api/tasks/{task_id}/human-decision", response_model=TaskResponse)
async def submit_human_decision(task_id: str, decision: HumanDecisionRequest):
    selected = decision.selected_option or decision.decision
    if not selected:
        raise _api_error("VALIDATION_ERROR", "A selected_option or decision is required.")
    try:
        return await orchestrator.submit_human_decision(task_id, selected)
    except KeyError:
        raise _api_error("TASK_NOT_FOUND", "Task not found.", 404)
    except Exception:
        logger.exception("human decision failed", extra={"task_id": task_id})
        raise _api_error("HUMAN_ACTION_FAILED", "The human decision could not be applied.", 500)


@app.get("/api/analytics")
async def get_analytics(authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    if firebase_service.enabled:
        return firebase_service.get_analytics(user_id)

    tasks = db_manager.get_all_tasks()
    steps = db_manager.get_all_steps()
    status_counts: Dict[str, int] = {}
    for task in tasks:
        status = task.get("status", "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1

    recoveries = 0
    human_interventions = 0
    for task in tasks:
        events = db_manager.get_execution_history(task["task_id"])
        recoveries += sum(1 for event in events if "RECOVERY" in str(event.get("event", "")) or "RETRY" in str(event.get("event", "")))
        human_interventions += sum(1 for event in events if "HUMAN" in str(event.get("event", "")))

    durations = []
    for task in tasks:
        try:
            created = datetime.fromisoformat(str(task.get("created_at")).replace("Z", ""))
            updated = datetime.fromisoformat(str(task.get("updated_at")).replace("Z", ""))
            durations.append((updated - created).total_seconds())
        except (TypeError, ValueError):
            continue

    return {
        "total_tasks": len(tasks),
        "completed_tasks": status_counts.get("COMPLETED", 0),
        "partially_completed_tasks": status_counts.get("PARTIALLY_COMPLETED", 0),
        "failed_tasks": status_counts.get("FAILED", 0),
        "average_execution_time": sum(durations) / len(durations) if durations else 0,
        "total_recoveries": recoveries,
        "total_human_interventions": human_interventions,
        "total_evidence_items": sum(len(step.get("evidence", [])) for step in steps),
        "documents_generated": sum(1 for task in tasks if _document_path_for_task(task)),
    }


@app.delete("/api/tasks/{task_id}")
async def archive_task(task_id: str, authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    if firebase_service.enabled:
        if firebase_service.archive_task(task_id, user_id):
            return {"task_id": task_id, "archived": True}
        raise _api_error("TASK_NOT_FOUND", "Task not found.", 404)
    if db_manager.delete_task(task_id):
        return {"task_id": task_id, "archived": False, "deleted": True}
    raise _api_error("TASK_NOT_FOUND", "Task not found.", 404)


@app.post("/api/files/upload")
async def upload_files(files: List[UploadFile] = File(...), authorization: Optional[str] = Header(None)):
    user_id = _request_user_id(authorization)
    UPLOAD_DIR.mkdir(exist_ok=True)
    saved_files = []

    for upload in files:
        original_name = upload.filename or "uploaded_file"
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            raise _api_error("UNSUPPORTED_FILE_TYPE", f"Unsupported file type: {suffix or 'none'}. Allowed: {allowed}")

        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(original_name).stem).strip("._") or "file"
        temp_path = UPLOAD_DIR / f"upload_{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size_bytes = 0

        try:
            with temp_path.open("wb") as output:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > MAX_UPLOAD_BYTES:
                        max_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
                        raise _api_error("FILE_TOO_LARGE", f"File too large: {original_name}. Maximum size is {max_mb} MB.")
                    digest.update(chunk)
                    output.write(chunk)
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise

        sha256 = digest.hexdigest()
        safe_name = secure_filename(safe_stem, suffix, sha256)
        file_path = UPLOAD_DIR / safe_name
        if file_path.exists():
            file_path.unlink()
        temp_path.replace(file_path)

        document_id = f"upload_{sha256[:16]}_{uuid.uuid4().hex[:8]}"
        metadata = build_metadata(
            document_id=document_id,
            path=file_path,
            user_id=user_id,
            content_type=upload.content_type,
            storage_path=(
                f"users/{user_id}/uploads/{document_id}/{safe_name}"
                if firebase_service.enabled and firebase_service.bucket
                else str(file_path.resolve())
            ),
        )
        metadata = record_local_document(metadata)
        if firebase_service.enabled:
            firebase_service.save_uploaded_document(metadata, file_path)
        verification = verify_local_document(document_id)
        if verification["integrity"] != "VERIFIED":
            raise _api_error("FILE_INTEGRITY_ERROR", f"Uploaded file failed SHA-256 integrity verification: {original_name}", 409)

        saved_files.append({
            "original_name": original_name,
            "file_name": safe_name,
            "file_path": str(file_path),
            "size_bytes": size_bytes,
            "content_type": upload.content_type,
            "document_id": document_id,
            "algorithm": "SHA-256",
            "sha256": sha256,
            "integrity": verification["integrity"],
            "version": metadata["version"],
        })

    return {"status": "uploaded", "files": saved_files}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
