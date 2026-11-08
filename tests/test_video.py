import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image

from upscale import main

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


@unittest.skipUnless(FFMPEG, "FFmpeg is not available")
class VideoUpscaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_video(self, name: str = "clip.mp4", with_audio: bool = True) -> Path:
        path = self.root / name
        command = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "testsrc=size=64x48:rate=5",
        ]
        if with_audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
        command += ["-t", "0.4", "-pix_fmt", "yuv420p", "-c:v", "libx264"]
        if with_audio:
            command += ["-c:a", "aac"]
        command.append(str(path))
        subprocess.run(command, capture_output=True, check=True)
        return path

    def run_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main([str(arg) for arg in args])

    def test_target_4k_is_exact_and_keeps_audio(self):
        source = self.make_video()
        output = self.root / "out" / "clip_4k.mp4"
        self.assertEqual(
            self.run_cli(source, "--target", "4k", "--crf", "40", "--preset", "ultrafast",
                         "-o", output),
            0,
        )
        data = probe(output)
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        audio = [stream for stream in data["streams"] if stream["codec_type"] == "audio"]
        self.assertEqual((video["width"], video["height"]), (3840, 2160))
        self.assertTrue(audio)

    def test_preserves_video_extension(self):
        source = self.make_video("clip.mkv")
        self.assertEqual(
            self.run_cli(source, "--target", "hd", "--crf", "40", "--preset", "ultrafast"),
            0,
        )
        output = self.root / "clip_hd.mkv"
        self.assertTrue(output.exists())
        data = probe(output)
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (1920, 1080))

    def test_scale_video_and_drop_audio(self):
        source = self.make_video()
        output = self.root / "scaled.mp4"
        self.assertEqual(
            self.run_cli(source, "--scale", "2", "--no-audio", "--crf", "40",
                         "--preset", "ultrafast", "-o", output),
            0,
        )
        data = probe(output)
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        audio = [stream for stream in data["streams"] if stream["codec_type"] == "audio"]
        self.assertEqual((video["width"], video["height"]), (128, 96))
        self.assertFalse(audio)

    def test_batch_directory_mixes_images_and_videos(self):
        source = self.make_video()
        Image.new("RGB", (32, 24), (10, 20, 30)).save(self.root / "photo.png")
        self.assertEqual(
            self.run_cli(self.root, "--target", "4k", "--crf", "40", "--preset", "ultrafast"),
            0,
        )
        output = self.root / "upscaled"
        self.assertTrue((output / "clip_4k.mp4").is_file())
        self.assertTrue((output / "photo_4k.png").is_file())
        with Image.open(output / "photo_4k.png") as image:
            self.assertEqual(image.size, (3840, 2160))

    def test_video_overwrite_protection(self):
        source = self.make_video()
        output = self.root / "clip_4k.mp4"
        self.assertEqual(
            self.run_cli(source, "--target", "4k", "--crf", "40", "--preset", "ultrafast",
                         "-o", output),
            0,
        )
        self.assertEqual(
            self.run_cli(source, "--target", "4k", "--crf", "40", "--preset", "ultrafast",
                         "-o", output),
            1,
        )

    def test_target_and_scale_are_mutually_exclusive(self):
        source = self.make_video()
        with self.assertRaises(SystemExit):
            self.run_cli(source, "--target", "4k", "--scale", "2")


if __name__ == "__main__":
    unittest.main()
