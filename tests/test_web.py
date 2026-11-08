"""HTTP tests for the local web interface."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from PIL import Image

_JOBS_DIR = Path(tempfile.mkdtemp(prefix="upscaler-web-tests-"))
os.environ["UPSCALER_JOBS_DIR"] = str(_JOBS_DIR)

import web  # noqa: E402  (reads UPSCALER_JOBS_DIR at import time)

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_png(width: int = 48, height: int = 32) -> io.BytesIO:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (180, 90, 50)).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def wait_for_job(client, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.get_data(as_text=True)
        job = response.get_json()
        if job["status"] in {"done", "error"}:
            return job
        time.sleep(0.15)
    raise AssertionError(f"Job {job_id} did not finish within {timeout} seconds.")


def wait_for_batch(client, batch_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/batch/{batch_id}")
        assert response.status_code == 200, response.get_data(as_text=True)
        batch = response.get_json()
        if batch["status"] == "done":
            return batch
        time.sleep(0.15)
    raise AssertionError(f"Batch {batch_id} did not finish within {timeout} seconds.")


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web.app.config.update(TESTING=True)
        cls.client = web.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        web._executor.shutdown(wait=True)
        shutil.rmtree(_JOBS_DIR, ignore_errors=True)

    def test_health_reports_ffmpeg(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertIsInstance(body["ffmpeg"], bool)

    def test_index_serves_the_ui(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Enlarger", response.get_data(as_text=True))
        response.close()

    def test_missing_file_is_rejected(self) -> None:
        response = self.client.post("/api/upscale", data={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_unsupported_image_extension_is_rejected(self) -> None:
        response = self.client.post(
            "/api/upscale",
            data={"kind": "image", "file": (io.BytesIO(b"nope"), "notes.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

    def test_target_and_scale_are_mutually_exclusive(self) -> None:
        response = self.client.post(
            "/api/upscale",
            data={
                "kind": "image",
                "target": "4k",
                "scale": "2",
                "file": (make_png(), "photo.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_target_is_rejected(self) -> None:
        response = self.client.post(
            "/api/upscale",
            data={"kind": "image", "target": "16k", "file": (make_png(), "photo.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

    def test_image_job_reaches_target_and_downloads(self) -> None:
        response = self.client.post(
            "/api/upscale",
            data={
                "kind": "image",
                "target": "hd",
                "sharpen": "0.5",
                "format": "png",
                "quality": "95",
                "file": (make_png(), "photo.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job = wait_for_job(self.client, response.get_json()["id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["after"], [1920, 1080])
        self.assertEqual(job["before"], [48, 32])
        self.assertTrue(job["output_name"].endswith("_hd.png"))

        download = self.client.get(job["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["Content-Type"], "image/png")
        with Image.open(io.BytesIO(download.data)) as result:
            self.assertEqual(result.size, (1920, 1080))
        download.close()

        preview = self.client.get(job["preview_url"])
        self.assertEqual(preview.status_code, 200)
        preview.close()

        deleted = self.client.delete(f"/api/jobs/{job['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}").status_code, 404)

    @unittest.skipUnless(FFMPEG, "FFmpeg is not available")
    def test_video_job_keeps_extension_and_audio(self) -> None:
        import subprocess
        import tempfile as tf

        with tf.TemporaryDirectory() as folder:
            source = Path(folder) / "clip.mkv"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=12:duration=0.5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(source),
                ],
                check=True,
            )
            with source.open("rb") as handle:
                response = self.client.post(
                    "/api/upscale",
                    data={
                        "kind": "video",
                        "target": "hd",
                        "sharpen": "0",
                        "crf": "30",
                        "preset": "ultrafast",
                        "keep_audio": "true",
                        "file": (io.BytesIO(handle.read()), "clip.mkv"),
                    },
                    content_type="multipart/form-data",
                )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job = wait_for_job(self.client, response.get_json()["id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["after"], [1920, 1080])
        self.assertIsNotNone(job["frames"])
        self.assertTrue(job["output_name"].endswith(".mkv"))

        download = self.client.get(job["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertGreater(len(download.data), 0)
        download.close()

    def test_unknown_job_returns_404(self) -> None:
        response = self.client.get("/api/jobs/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())

    def test_batch_requires_at_least_one_file(self) -> None:
        response = self.client.post(
            "/api/batch",
            data={"kind": "image"},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_batch_rejects_a_wrong_extension(self) -> None:
        response = self.client.post(
            "/api/batch",
            data={
                "kind": "image",
                "files": [(make_png(), "good.png"), (io.BytesIO(b"nope"), "notes.txt")],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("notes.txt", response.get_json()["error"])

    def test_batch_processes_every_file_and_zips_them(self) -> None:
        response = self.client.post(
            "/api/batch",
            data={
                "kind": "image",
                "target": "hd",
                "format": "png",
                "files": [(make_png(), "photo.png"), (make_png(20, 20), "photo.png")],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        created = response.get_json()
        self.assertEqual(created["total"], 2)
        self.assertEqual(created["completed"], 0)
        self.assertTrue(
            all(item["status"] in {"queued", "processing"} for item in created["items"])
        )

        batch = wait_for_batch(self.client, created["id"])
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["completed"], 2)
        self.assertEqual(batch["failed"], 0)
        self.assertTrue(all(item["status"] == "done" for item in batch["items"]))
        self.assertEqual({tuple(item["after"]) for item in batch["items"]}, {(1920, 1080)})
        names = {item["output_name"] for item in batch["items"]}
        self.assertEqual(len(names), 2, names)
        self.assertTrue(all(name.endswith("_hd.png") for name in names))

        item = batch["items"][0]
        single = self.client.get(item["download_url"])
        self.assertEqual(single.status_code, 200)
        with Image.open(io.BytesIO(single.data)) as result:
            self.assertEqual(result.size, (1920, 1080))
        single.close()

        preview = self.client.get(item["preview_url"])
        self.assertEqual(preview.status_code, 200)
        preview.close()

        archive = self.client.get(batch["zip_url"])
        self.assertEqual(archive.status_code, 200)
        self.assertIn("zip", archive.headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
            self.assertEqual(sorted(bundle.namelist()), sorted(names))
            for entry in bundle.namelist():
                self.assertGreater(bundle.getinfo(entry).file_size, 0)
        archive.close()

        deleted = self.client.delete(f"/api/batch/{batch['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get(f"/api/batch/{batch['id']}").status_code, 404)

    def test_unknown_batch_returns_404(self) -> None:
        response = self.client.get("/api/batch/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())


if __name__ == "__main__":
    unittest.main()
