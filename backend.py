"""转文字工坊 - FastAPI backend.

API:
    POST   /api/jobs                 新建任务 (form: url=... / file=..., model, lang, initial_prompt)
    GET    /api/jobs                 任务列表
    GET    /api/jobs/{jid}           任务详情（含文稿预览）
    GET    /api/jobs/{jid}/download?format=txt|md|srt|vtt   下载文稿
    DELETE /api/jobs/{jid}           删除任务
    GET    /                         前端页面
"""
import json
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import pipeline

BASE = Path(__file__).resolve().parent
DATA = BASE / "data" / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
FRONTEND = BASE / "frontend"

@asynccontextmanager
async def lifespan(app):
    _recover_interrupted_jobs()
    yield


app = FastAPI(title="转文字工坊", lifespan=lifespan)
pool = ThreadPoolExecutor(max_workers=1)
_save_lock = threading.RLock()

MODELS = {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"}
LANGUAGES = {"zh", "auto", "en"}
METHODS = {"auto", "speech"}
ACTIVE_STATUSES = {"queued", "working"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
FORMAT_EXT = {"txt": ".txt", "md": ".md", "srt": ".srt", "vtt": ".vtt"}
FORMAT_MIME = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "srt": "text/plain; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
}


def _job_dir(jid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", jid):
        raise HTTPException(400, "无效的任务编号")
    path = DATA / jid
    if path.is_symlink() or path.resolve().parent != DATA.resolve():
        raise HTTPException(400, "无效的任务目录")
    return path


def _job_path(jid: str) -> Path:
    return _job_dir(jid) / "job.json"


def _save_job(job: dict) -> None:
    with _save_lock:
        path = _job_path(job["id"])
        pending = path.with_suffix(".json.tmp")
        pending.write_text(
            json.dumps(job, ensure_ascii=False), encoding="utf-8")
        pending.replace(path)


def _load_job(jid: str) -> dict:
    p = _job_path(jid)
    if not p.exists():
        raise HTTPException(404, "任务不存在")
    return json.loads(p.read_text(encoding="utf-8"))


def _recover_interrupted_jobs() -> None:
    """A single local server owns the queue; old active jobs cannot resume."""
    for path in DATA.glob("*/job.json"):
        try:
            job = _load_job(path.parent.name)
            if job["status"] in ACTIVE_STATUSES:
                job["status"] = "error"
                job["message"] = "服务重启，任务已中断。请重新提交；已有文件已保留。"
                job["interrupted_at"] = time.time()
                _save_job(job)
        except (HTTPException, OSError, ValueError, KeyError, TypeError):
            # One damaged record must not prevent all other jobs from opening.
            continue


def _public_job(job: dict) -> dict:
    """List view: no transcript body."""
    return {
        "id": job["id"],
        "title": job.get("title", ""),
        "source": job.get("source", ""),
        "status": job["status"],
        "message": job.get("message", ""),
        "model": job.get("model", ""),
        "method": job.get("method", "auto"),
        "requested_language": job.get("requested_language", "zh"),
        "language": job.get("language", ""),
        "engine": job.get("engine", ""),
        "review_count": job.get("review_count", 0),
        "quality_notes": job.get("quality_notes", []),
        "total_segments": job.get("total_segments", 0),
        "formats": job.get("formats", []),
        "created": job.get("created", 0),
    }


def _run_job(jid: str) -> None:
    job = _load_job(jid)
    tmpdir = None
    try:
        job["status"] = "working"
        download_label = "下载视频…" if job.get("method", "auto") == "auto" else "下载音频…"
        job["message"] = download_label if job["source"] == "url" else "准备转写…"
        _save_job(job)

        if job["source"] == "url":
            tmpdir = tempfile.mkdtemp(prefix="vsdl_")

            def hook(d):
                if d.get("status") == "downloading":
                    pct = (d.get("_percent_str") or "").strip()
                    eta = (d.get("_eta_str") or "").strip()
                    job["message"] = f"{download_label} {pct} {eta}".strip()
                    _save_job(job)

            download = (pipeline.download_media if job.get("method", "auto") == "auto"
                        else pipeline.download_audio)
            audio_path, title = download(
                job["input"], tmpdir, progress_hook=hook)
            job["title"] = title
        else:
            audio_path = job["input"]
            tmpdir = None

        job["message"] = ("检查画面字幕…" if job.get("method", "auto") == "auto"
                          else "语音转写中…（首次运行需下载模型，请耐心等待）")
        _save_job(job)

        def on_segment(n):
            if n % 20 == 0:
                job["message"] = f"转写中…已生成 {n} 段"
                _save_job(job)

        def on_status(message):
            job["message"] = str(message)
            _save_job(job)

        segments, info = pipeline.transcribe_file(
            audio_path, model_size=job.get("model", "large-v3-turbo"),
            lang=job.get("requested_language", "zh"),
            initial_prompt=job.get("initial_prompt", ""),
            method=job.get("method", "auto"),
            on_status=on_status,
            on_segment=on_segment)

        outdir = _job_dir(jid)
        paths = pipeline.write_outputs(segments, job["title"], str(outdir))
        (outdir / "segments.json").write_text(
            json.dumps({"segments": segments, "info": info}, ensure_ascii=False, indent=2),
            encoding="utf-8")

        job["status"] = "done"
        job["message"] = f"完成，共 {len(segments)} 段"
        job["language"] = info.get("language", "")
        job["engine"] = info.get("engine", "")
        job["review_count"] = info.get("review_count", 0)
        job["quality_notes"] = info.get("quality_notes", [])
        job["quality_report"] = info.get("quality_report", {})
        job["total_segments"] = len(segments)
        job["segments"] = segments
        job["formats"] = list(paths.keys())
        _save_job(job)
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        job["status"] = "error"
        job["message"] = f"失败：{e}"
        _save_job(job)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/jobs")
def create_job(url: str = Form(default=None),
               model: str = Form(default="large-v3-turbo"),
               lang: str = Form(default="zh"),
               method: str = Form(default="auto"),
               initial_prompt: str = Form(default=""),
               file: UploadFile = File(default=None)):
    if model not in MODELS:
        raise HTTPException(400, "不支持的模型")
    if lang not in LANGUAGES:
        raise HTTPException(400, "不支持的识别语言")
    if method not in METHODS:
        raise HTTPException(400, "不支持的识别方式")
    initial_prompt = initial_prompt.strip()
    if len(initial_prompt) > 2000:
        raise HTTPException(400, "术语提示不能超过 2000 字符")
    url = (url or "").strip()
    has_file = bool(file and file.filename)
    if bool(url) == has_file:
        raise HTTPException(400, "请只提供一个链接或一个文件")
    if file and not file.filename:
        raise HTTPException(400, "请提供链接或上传文件")
    if url:
        try:
            parsed = urlsplit(url)
            valid = (len(url) <= 8192 and parsed.scheme in {"https", "http"}
                     and parsed.hostname and not parsed.username and not parsed.password
                     and not any(c.isspace() for c in url))
            parsed.port  # Validate malformed port syntax before submitting a task.
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(400, "请输入有效的 HTTP 或 HTTPS 公开链接")

    jid = uuid.uuid4().hex[:12]
    jobdir = _job_dir(jid)
    jobdir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": jid,
        "title": "",
        "source": "",
        "input": "",
        "status": "queued",
        "message": "排队中…",
        "model": model,
        "method": method,
        "requested_language": lang,
        "initial_prompt": initial_prompt,
        "language": "",
        "total_segments": 0,
        "segments": [],
        "formats": [],
        "created": time.time(),
    }
    try:
        if has_file:
            ext = Path(file.filename).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,12}", ext):
                ext = ".bin"
            dest = jobdir / ("upload" + ext)
            size = 0
            with dest.open("wb") as output:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "上传文件不能超过 2 GB")
                    output.write(chunk)
            if not size:
                raise HTTPException(400, "上传文件为空")
            job["source"] = "upload"
            job["input"] = str(dest)
            job["title"] = Path(file.filename).name
        else:
            job["source"] = "url"
            job["input"] = url
            job["title"] = url
        _save_job(job)
        pool.submit(_run_job, jid)
    except Exception:
        shutil.rmtree(jobdir, ignore_errors=True)
        raise
    return {"id": jid}


@app.get("/api/jobs")
def list_jobs():
    jobs = []
    for p in DATA.glob("*/job.json"):
        try:
            _job_dir(p.parent.name)
            jobs.append(_public_job(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    jobs.sort(key=lambda j: j["created"], reverse=True)
    return jobs


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return _load_job(jid)


@app.get("/api/jobs/{jid}/download")
def download_job(jid: str, format: str = "txt"):
    if format not in FORMAT_EXT:
        raise HTTPException(400, "不支持的格式")
    job = _load_job(jid)
    if job["status"] != "done":
        raise HTTPException(409, "任务尚未完成")
    stem = pipeline.safe_filename(job["title"])
    path = _job_dir(jid) / (stem + FORMAT_EXT[format])
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(
        path, media_type=FORMAT_MIME[format],
        filename=path.name,
    )


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    with _save_lock:
        job = _load_job(jid)
        if job["status"] in ACTIVE_STATUSES:
            raise HTTPException(409, "任务正在排队或处理中，请完成后再删除")
        shutil.rmtree(_job_dir(jid))
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
