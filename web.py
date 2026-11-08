"""Local web UI for the image and video upscaler.

Run with:
    python web.py
or
    image-upscaler-web
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from upscale import (
    EXTENSIONS,
    RESOLUTION_PRESETS,
    VIDEO_EXTENSIONS,
    upscale,
    upscale_video,
)

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
JOBS_DIR = Path(os.environ.get("UPSCALER_JOBS_DIR", BASE_DIR / "jobs"))
MAX_UPLOAD = 2 * 1024 * 1024 * 1024  # 2 GiB
PRESETS = {"ultrafast", "veryfast", "fast", "medium", "slow", "veryslow"}
IMAGE_FORMATS = {"png", "jpg", "webp", "bmp", "tiff"}
BATCH_LIMIT = 50

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_batches: dict[str, dict] = {}
_batches_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1)


def _public_job(job: dict) -> dict:
    done = job.get("status") == "done"
    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "progress": job.get("progress"),
        "original_name": job.get("original_name"),
        "output_name": job.get("output_name"),
        "download_url": f"/api/jobs/{job['id']}/download" if done else None,
        "preview_url": f"/api/jobs/{job['id']}/preview" if done else None,
        "before": job.get("before"),
        "after": job.get("after"),
        "frames": job.get("frames"),
        "error": job.get("error"),
    }


def _update_job(job_id: str, **changes) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(changes)


def _get_job(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        abort(404, description="Job not found.")
    return job


def _form_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = request.form.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        abort(400, description=f"{name} must be a number.")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        abort(400, description=f"{name} must be between {minimum:g} and {maximum:g}.")
    return value


def _form_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.form.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        abort(400, description=f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        abort(400, description=f"{name} must be between {minimum} and {maximum}.")
    return value


def _friendly_error(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    message = message.replace(str(JOBS_DIR) + os.sep, "")
    lowered = message.lower()
    if type(error).__name__ == "UnidentifiedImageError" or "cannot identify image file" in lowered:
        return "That file could not be decoded as an image. It may be corrupt or in an unsupported format."
    if len(message) > 500:
        message = message[:500].rstrip() + "…"
    return message


def _cleanup_old_jobs(max_age_hours: int = 24) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max_age_hours * 3600
    for path in JOBS_DIR.iterdir():
        if not path.is_dir():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    stale = lambda entry: entry.get("created_at", 0) < cutoff and entry.get("status") != "processing"  # noqa: E731
    with _jobs_lock:
        for key in [k for k, job in _jobs.items() if stale(job)]:
            _jobs.pop(key, None)
    with _batches_lock:
        for key in [k for k, batch in _batches.items() if stale(batch)]:
            _batches.pop(key, None)


def _run_upscale(
    kind: str,
    source: Path,
    output: Path,
    options: dict,
    update,
) -> None:
    if kind == "image":
        before, after = upscale(
            source,
            output,
            options["scale"] or 2.0,
            options["sharpen"],
            options["quality"],
            overwrite=True,
            target_res=options["target"],
        )
        update(status="done", progress=1.0, before=list(before), after=list(after))
        return

    def on_progress(done: int, total: int | None) -> None:
        value = min(0.99, done / total) if total else None
        update(progress=value, frames=done)

    before, after, frames = upscale_video(
        source,
        output,
        scale=options["scale"],
        target_res=options["target"],
        sharpen=options["sharpen"],
        crf=options["crf"],
        preset=options["preset"],
        overwrite=True,
        keep_audio=options["keep_audio"],
        progress_callback=on_progress,
    )
    update(
        status="done",
        progress=1.0,
        before=list(before),
        after=list(after),
        frames=frames,
    )


def _process_job(
    job_id: str,
    source: Path,
    output: Path,
    kind: str,
    options: dict,
    output_name: str,
) -> None:
    _update_job(job_id, status="processing", progress=0.02)
    try:
        _run_upscale(
            kind,
            source,
            output,
            options,
            lambda **changes: _update_job(job_id, **changes),
        )
        _update_job(job_id, output_name=output_name)
    except Exception as error:  # noqa: BLE001 - the message is shown to the user
        _update_job(
            job_id,
            status="error",
            progress=None,
            error=_friendly_error(error),
        )
    finally:
        try:
            source.unlink(missing_ok=True)
        except OSError:
            pass


@app.errorhandler(HTTPException)
def _http_error(error: HTTPException):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description), error.code
    return error


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify(
        status="ok",
        ffmpeg=bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
    )


def _read_options(kind: str) -> dict:
    target = request.form.get("target", "").strip().lower() or None
    if target is not None and target not in RESOLUTION_PRESETS:
        abort(400, description="Unknown target resolution.")
    scale_raw = request.form.get("scale", "").strip()
    if target is not None and scale_raw:
        abort(400, description="Use either target or scale, not both.")
    scale = None if target is not None else _form_float("scale", 2.0, 1.0, 1000.0)

    options: dict = {
        "target": target,
        "scale": scale,
        "sharpen": _form_float("sharpen", 0.0, 0.0, 3.0),
    }

    if kind == "image":
        image_format = request.form.get("format", "png").strip().lower()
        if image_format not in IMAGE_FORMATS:
            abort(400, description="Unsupported image output format.")
        options["format"] = image_format
        options["quality"] = _form_int("quality", 95, 1, 100)
    else:
        preset = request.form.get("preset", "medium").strip().lower()
        if preset not in PRESETS:
            abort(400, description="Unknown video preset.")
        options["crf"] = _form_int("crf", 23, 0, 51)
        options["preset"] = preset
        options["keep_audio"] = request.form.get("keep_audio", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }
    return options


def _output_tag(options: dict) -> str:
    if options["target"]:
        return options["target"]
    scale = options["scale"]
    return f"{format(scale if scale is not None else 2.0, '.12g')}x"


def _output_extension(kind: str, options: dict, source_extension: str) -> str:
    return "." + options["format"] if kind == "image" else source_extension


def _unique_stem(stem: str, used: dict[str, int]) -> str:
    seen = used.get(stem, 0)
    used[stem] = seen + 1
    return stem if seen == 0 else f"{stem} ({seen + 1})"


@app.post("/api/upscale")
def create_job():
    _cleanup_old_jobs()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        abort(400, description="Choose an image or video to upscale.")

    raw_name = Path(upload.filename).name
    extension = Path(raw_name).suffix.lower()
    original_name = secure_filename(raw_name)
    if not original_name:
        original_name = f"upload{extension}"

    kind = request.form.get("kind", "").strip().lower()
    if kind not in {"image", "video"}:
        kind = "video" if extension in VIDEO_EXTENSIONS else "image"

    if kind == "image" and extension not in EXTENSIONS:
        abort(400, description="Unsupported image type. Use PNG, JPG, WebP, BMP, or TIFF.")
    if kind == "video" and extension not in VIDEO_EXTENSIONS:
        abort(400, description="Unsupported video type. Use MP4, MKV, MOV, AVI, or WebM.")

    options = _read_options(kind)
    out_extension = _output_extension(kind, options, extension)

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    source = job_dir / f"source{extension}"
    try:
        upload.save(source)
    except OSError as error:
        shutil.rmtree(job_dir, ignore_errors=True)
        abort(500, description=f"Could not store the upload: {error}")

    output_name = f"{Path(original_name).stem}_{_output_tag(options)}{out_extension}"
    output = job_dir / f"output{out_extension}"

    job = {
        "id": job_id,
        "kind": kind,
        "status": "queued",
        "progress": 0.0,
        "original_name": original_name,
        "output_name": output_name,
        "job_dir": job_dir,
        "output_path": output,
        "before": None,
        "after": None,
        "frames": None,
        "error": None,
        "created_at": time.time(),
    }
    with _jobs_lock:
        _jobs[job_id] = job

    _executor.submit(_process_job, job_id, source, output, kind, options, output_name)
    return jsonify(_public_job(job)), 202


# ---------- batches ----------


def _public_item(batch_id: str, item: dict) -> dict:
    done = item["status"] == "done"
    return {
        "id": item["id"],
        "original_name": item["original_name"],
        "output_name": item["output_name"],
        "status": item["status"],
        "progress": item.get("progress"),
        "before": item.get("before"),
        "after": item.get("after"),
        "frames": item.get("frames"),
        "error": item.get("error"),
        "download_url": f"/api/batch/{batch_id}/items/{item['id']}/download" if done else None,
        "preview_url": f"/api/batch/{batch_id}/items/{item['id']}/preview" if done else None,
    }


def _batch_progress(batch: dict) -> float:
    items = batch["items"]
    if not items:
        return 0.0
    total = 0.0
    for item in items:
        if item["status"] in {"done", "error"}:
            total += 1.0
        elif item["status"] == "processing":
            total += min(0.99, max(0.02, item.get("progress") or 0.02))
    return min(1.0, total / len(items))


def _public_batch(batch: dict) -> dict:
    items = batch["items"]
    completed = sum(1 for item in items if item["status"] == "done")
    failed = sum(1 for item in items if item["status"] == "error")
    status = batch["status"]
    if status != "processing" and completed + failed == len(items):
        status = "done"
    return {
        "id": batch["id"],
        "kind": batch["kind"],
        "status": status,
        "progress": 1.0 if status == "done" else _batch_progress(batch),
        "total": len(items),
        "completed": completed,
        "failed": failed,
        "zip_url": f"/api/batch/{batch['id']}/download" if completed else None,
        "zip_name": f"enlarged-{batch['kind']}s.zip" if completed else None,
        "items": [_public_item(batch["id"], item) for item in items],
    }


def _get_batch(batch_id: str) -> dict:
    with _batches_lock:
        batch = _batches.get(batch_id)
    if batch is None:
        abort(404, description="Batch not found.")
    return batch


def _update_batch(batch_id: str, **changes) -> None:
    with _batches_lock:
        batch = _batches.get(batch_id)
        if batch is not None:
            batch.update(changes)


def _update_item(batch_id: str, item_id: str, **changes) -> None:
    with _batches_lock:
        batch = _batches.get(batch_id)
        if batch is None:
            return
        for item in batch["items"]:
            if item["id"] == item_id:
                item.update(changes)
                return


def _item_updater(batch_id: str, item_id: str):
    def update(**changes) -> None:
        _update_item(batch_id, item_id, **changes)

    return update


def _process_batch(batch_id: str) -> None:
    with _batches_lock:
        batch = _batches.get(batch_id)
    if batch is None:
        return

    kind = batch["kind"]
    options = batch["options"]
    _update_batch(batch_id, status="processing")

    for item in batch["items"]:
        item_id = item["id"]
        _update_item(batch_id, item_id, status="processing", progress=0.02)
        try:
            _run_upscale(
                kind,
                item["source_path"],
                item["output_path"],
                options,
                _item_updater(batch_id, item_id),
            )
        except Exception as error:  # noqa: BLE001 - the message is shown to the user
            _update_item(
                batch_id,
                item_id,
                status="error",
                progress=None,
                error=_friendly_error(error),
            )
        finally:
            try:
                item["source_path"].unlink(missing_ok=True)
            except OSError:
                pass

    _update_batch(batch_id, status="done")


def _batch_item(batch_id: str, item_id: str) -> tuple[dict, dict]:
    batch = _get_batch(batch_id)
    for item in batch["items"]:
        if item["id"] == item_id:
            return batch, item
    abort(404, description="File not found in this batch.")


@app.post("/api/batch")
def create_batch():
    _cleanup_old_jobs()
    uploads = [f for f in request.files.getlist("files") if f is not None and f.filename]
    if not uploads:
        abort(400, description="Choose at least one file to upscale.")
    if len(uploads) > BATCH_LIMIT:
        abort(400, description=f"Upscale up to {BATCH_LIMIT} files in one batch.")

    kind = request.form.get("kind", "").strip().lower()
    if kind not in {"image", "video"}:
        abort(400, description="Choose image or video files for the batch.")

    entries = []
    for upload in uploads:
        raw_name = Path(upload.filename).name
        extension = Path(raw_name).suffix.lower()
        if kind == "image" and extension not in EXTENSIONS:
            abort(
                400,
                description=(
                    f"{raw_name} is not an image this tool can read. "
                    "Use PNG, JPG, WebP, BMP, or TIFF."
                ),
            )
        if kind == "video" and extension not in VIDEO_EXTENSIONS:
            abort(
                400,
                description=(
                    f"{raw_name} is not a video this tool can read. "
                    "Use MP4, MKV, MOV, AVI, or WebM."
                ),
            )
        safe_name = secure_filename(raw_name) or f"file{extension}"
        entries.append((upload, extension, safe_name))

    options = _read_options(kind)

    batch_id = uuid.uuid4().hex
    batch_dir = JOBS_DIR / batch_id
    items_dir = batch_dir / "items"
    items_dir.mkdir(parents=True, exist_ok=False)

    used: dict[str, int] = {}
    items = []
    try:
        for upload, extension, safe_name in entries:
            item_id = uuid.uuid4().hex[:12]
            item_dir = items_dir / item_id
            item_dir.mkdir()
            source = item_dir / f"source{extension}"
            upload.save(source)

            item_extension = _output_extension(kind, options, extension)
            stem = _unique_stem(Path(safe_name).stem, used)
            output_name = f"{stem}_{_output_tag(options)}{item_extension}"
            items.append(
                {
                    "id": item_id,
                    "original_name": safe_name,
                    "output_name": output_name,
                    "status": "queued",
                    "progress": 0.0,
                    "source_path": source,
                    "output_path": item_dir / f"output{item_extension}",
                    "before": None,
                    "after": None,
                    "frames": None,
                    "error": None,
                }
            )
    except OSError as error:
        shutil.rmtree(batch_dir, ignore_errors=True)
        abort(500, description=f"Could not store the uploads: {error}")

    batch = {
        "id": batch_id,
        "kind": kind,
        "status": "queued",
        "batch_dir": batch_dir,
        "options": options,
        "items": items,
        "created_at": time.time(),
    }
    with _batches_lock:
        _batches[batch_id] = batch

    _executor.submit(_process_batch, batch_id)
    return jsonify(_public_batch(batch)), 202


@app.get("/api/batch/<batch_id>")
def get_batch(batch_id: str):
    return jsonify(_public_batch(_get_batch(batch_id)))


@app.get("/api/batch/<batch_id>/items/<item_id>/download")
def download_item(batch_id: str, item_id: str):
    _, item = _batch_item(batch_id, item_id)
    if item["status"] != "done":
        abort(409, description="That file is not finished yet.")
    path = item["output_path"]
    if not path.is_file():
        abort(404, description="The output file is no longer available.")
    return send_file(
        path,
        as_attachment=True,
        download_name=item["output_name"],
        conditional=True,
    )


@app.get("/api/batch/<batch_id>/items/<item_id>/preview")
def preview_item(batch_id: str, item_id: str):
    _, item = _batch_item(batch_id, item_id)
    if item["status"] != "done":
        abort(409, description="That file is not finished yet.")
    path = item["output_path"]
    if not path.is_file():
        abort(404, description="The output file is no longer available.")
    return send_file(
        path,
        as_attachment=False,
        download_name=item["output_name"],
        conditional=True,
    )


@app.get("/api/batch/<batch_id>/download")
def download_batch(batch_id: str):
    batch = _get_batch(batch_id)
    finished = [
        item for item in batch["items"]
        if item["status"] == "done" and item["output_path"].is_file()
    ]
    if not finished:
        abort(409, description="No finished files in this batch yet.")

    archive_path = batch["batch_dir"] / "batch.zip"
    if not archive_path.is_file():
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
            for item in finished:
                archive.write(item["output_path"], arcname=item["output_name"])

    return send_file(
        archive_path,
        as_attachment=True,
        download_name=f"enlarged-{batch['kind']}s.zip",
        conditional=True,
    )


@app.delete("/api/batch/<batch_id>")
def delete_batch(batch_id: str):
    with _batches_lock:
        batch = _batches.get(batch_id)
        if batch is None:
            abort(404, description="Batch not found.")
        if batch["status"] == "processing":
            abort(409, description="Wait for the batch to finish before deleting it.")
        _batches.pop(batch_id, None)
    shutil.rmtree(batch["batch_dir"], ignore_errors=True)
    return jsonify(ok=True)


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    return jsonify(_public_job(_get_job(job_id)))


def _done_job_file(job_id: str) -> tuple[dict, Path]:
    job = _get_job(job_id)
    if job["status"] != "done":
        abort(409, description="The job is not finished yet.")
    path = job["output_path"]
    if not path.is_file():
        abort(404, description="The output file is no longer available.")
    return job, path


@app.get("/api/jobs/<job_id>/download")
def download(job_id: str):
    job, path = _done_job_file(job_id)
    return send_file(
        path,
        as_attachment=True,
        download_name=job["output_name"],
        conditional=True,
    )


@app.get("/api/jobs/<job_id>/preview")
def preview(job_id: str):
    job, path = _done_job_file(job_id)
    return send_file(
        path,
        as_attachment=False,
        download_name=job["output_name"],
        conditional=True,
    )


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            abort(404, description="Job not found.")
        if job["status"] == "processing":
            abort(409, description="Wait for the job to finish before deleting it.")
        _jobs.pop(job_id, None)
    shutil.rmtree(job["job_dir"], ignore_errors=True)
    return jsonify(ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local web UI for the image and video upscaler.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=5000, help="Port")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode")
    args = parser.parse_args(argv)

    _cleanup_old_jobs()
    print(f"Image & Video Upscaler UI: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
