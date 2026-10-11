"""Local captions-first transcription with speech fallback and honest provenance."""
import math
import os
import re
import threading
from pathlib import Path

import yt_dlp

# --- compatibility shim: some PyAV releases removed av.open(metadata_errors=...)
import av as _av


def _av_needs_patch() -> bool:
    try:
        c = _av.open(__file__, mode="r", metadata_errors="ignore")
        c.close()
        return False
    except TypeError:
        return True
    except Exception:
        return False


if _av_needs_patch():
    _orig_av_open = _av.open

    def _av_open_compat(file, *args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return _orig_av_open(file, *args, **kwargs)

    _av.open = _av_open_compat
del _av

from faster_whisper import WhisperModel  # noqa: E402

_models: dict = {}
_models_lock = threading.Lock()
BASE = Path(__file__).resolve().parent


def get_model(size: str = "large-v3-turbo") -> WhisperModel:
    """Load a packaged model when present; otherwise use the normal HF cache."""
    with _models_lock:
        if size not in _models:
            # Jobs run serially; retaining several large models only wastes RAM.
            _models.clear()
            local = BASE / "runtime" / "models" / size
            source = str(local) if (local / "model.bin").is_file() else size
            _models[size] = WhisperModel(
                source, device="auto", compute_type="int8", cpu_threads=4)
        return _models[size]


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return (name or "transcript")[:80]


def fmt_srt_time(sec: float) -> str:
    if not math.isfinite(sec):
        raise ValueError("时间戳必须为有限数值")
    total_ms = max(0, round(sec * 1000))
    seconds, ms = divmod(total_ms, 1000)
    minutes, s = divmod(seconds, 60)
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_vtt_time(sec: float) -> str:
    return fmt_srt_time(sec).replace(",", ".")


def _download(url: str, tmpdir: str, *, video: bool, progress_hook=None):
    """Download one public item and return the final media file, not fragments."""
    directory = Path(tmpdir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = "media" if video else "audio"
    ydl_opts = {
        "format": ("bestvideo[height<=720][vcodec^=avc1]+bestaudio/"
                   "best[height<=720]/best") if video else "bestaudio/best",
        "outtmpl": str(directory / (stem + ".%(ext)s")),
        "noplaylist": True,
        "playlistend": 1,
        "quiet": True,
        "nocolor": True,
    }
    if video:
        ydl_opts["merge_output_format"] = "mp4"
    if (BASE / "runtime" / "bin" / "ffmpeg").is_file():
        ydl_opts["ffmpeg_location"] = str(BASE / "runtime" / "bin")
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info or info.get("_type") in {"playlist", "multi_video"}:
            raise RuntimeError("请提供单个视频或音频的链接")
        prepared = Path(ydl.prepare_filename(info))
    title = info.get("title") or "transcript"
    candidates = ([directory / "media.mp4"] if video else []) + [prepared]
    candidates += sorted(directory.glob(stem + ".*"))
    for path in candidates:
        # yt-dlp fragment names contain extra suffixes such as .f30280.m4a.
        if (path.parent.resolve() == directory.resolve() and path.is_file()
                and path.stat().st_size > 0 and path.stem == stem
                and path.suffix not in {".part", ".ytdl", ".json", ".tmp"}):
            return str(path), title
    raise RuntimeError("下载未生成完整的媒体文件")


def download_audio(url: str, tmpdir: str, progress_hook=None):
    """Keep native audio; PyAV can decode it without a lossy MP3 conversion."""
    return _download(url, tmpdir, video=False, progress_hook=progress_hook)


def download_media(url: str, tmpdir: str, progress_hook=None):
    """Download video plus audio so displayed captions remain available."""
    return _download(url, tmpdir, video=True, progress_hook=progress_hook)


def _validate_segments(segments):
    if not segments:
        raise RuntimeError("未识别出可用内容")
    previous_start = -1.0
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        if (not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end < start or start < previous_start
                or not str(segment["text"]).strip()):
            raise RuntimeError("识别结果包含无效文稿或时间戳")
        previous_start = start


def transcribe_file(path: str, model_size: str = "large-v3-turbo", lang="zh",
                     on_segment=None, initial_prompt=None, method="auto", on_status=None):
    """Transcribe an audio/video file.

    Returns (segments, info) where segments is a list of
    {"start": float, "end": float, "text": str}.
    """
    if method not in {"auto", "speech"}:
        raise ValueError("不支持的识别方式")
    fallback_note = None
    if method == "auto":
        try:
            from burned_captions import extract
            captions, caption_info = extract(path, on_status=on_status)
            _validate_segments(captions)
            if on_segment:
                on_segment(len(captions))
            return captions, caption_info
        except Exception as exc:
            fallback_note = "画面字幕未采用：" + str(exc)[:400]
            if on_status:
                on_status(fallback_note + "；转为语音识别…")
    if on_status:
        on_status("加载语音模型…首次使用可能需要下载")
    model = get_model(model_size)
    if on_status:
        on_status("语音转写中…")
    segments, info = model.transcribe(
        path, language=None if lang in (None, "auto", "") else lang,
        initial_prompt=(initial_prompt or "").strip() or None,
        beam_size=5, vad_filter=True)
    out = []
    review_segments = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        item = {
            "start": float(seg.start),
            "end": float(seg.end),
            "text": text,
        }
        reasons = []
        for key in ("avg_logprob", "no_speech_prob", "compression_ratio"):
            value = getattr(seg, key, None)
            if value is not None and math.isfinite(value):
                item[key] = round(float(value), 4)
        if item.get("avg_logprob", 0) < -0.6:
            reasons.append("语音识别置信度较低")
        if item.get("no_speech_prob", 0) > 0.5:
            reasons.append("可能不是有效语音")
        if item.get("compression_ratio", 0) > 2.4:
            reasons.append("可能包含异常重复")
        if reasons:
            item["review_reasons"] = reasons
            review_segments.append(len(out) + 1)
        out.append(item)
        if on_segment:
            on_segment(len(out))
    _validate_segments(out)
    notes = ["本结果来自语音识别，未用生成式改写替代原话。",
             "需复核提示来自模型信号，不等于文字准确率；数字、专有名词和否定词仍需核对。"]
    if fallback_note:
        notes.insert(0, fallback_note)
    return out, {
        "language": info.language,
        "probability": round(info.language_probability, 4),
        "engine": "语音识别 · " + model_size,
        "review_count": len(review_segments),
        "quality_notes": notes,
        "quality_report": {"method": "faster-whisper", "model": model_size,
                           "review_segments": review_segments, "accuracy": None},
    }


def write_outputs(segments, title: str, outdir: str) -> dict:
    """Write TXT/MD/SRT/VTT. Returns {format: path}."""
    Path(outdir).mkdir(parents=True, exist_ok=True)
    stem = safe_filename(title)
    paths = {
        "txt": os.path.join(outdir, stem + ".txt"),
        "md": os.path.join(outdir, stem + ".md"),
        "srt": os.path.join(outdir, stem + ".srt"),
        "vtt": os.path.join(outdir, stem + ".vtt"),
    }
    with open(paths["txt"], "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(seg["text"] + "\n")

    with open(paths["md"], "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        for seg in segments:
            f.write(f"**[{fmt_srt_time(seg['start'])}]** {seg['text']}\n\n")

    with open(paths["srt"], "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n{fmt_srt_time(seg['start'])} --> "
                    f"{fmt_srt_time(seg['end'])}\n{seg['text']}\n\n")

    with open(paths["vtt"], "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for seg in segments:
            f.write(f"{fmt_vtt_time(seg['start'])} --> "
                    f"{fmt_vtt_time(seg['end'])}\n{seg['text']}\n\n")
    return paths
