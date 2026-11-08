"""Local image and video upscaling CLI using Pillow Lanczos and FFmpeg."""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
           ".webp": "WEBP", ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF"}
MAX_PIXELS = 80_000_000

RESOLUTION_PRESETS = {
    "hd": (1920, 1080),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
    "8k": (7680, 4320),
}


def scale_value(value: str) -> float:
    try:
        scale = float(value.lower().removesuffix("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("Scale must be a number, for example: 2, 4, or 2.5.") from None
    if not math.isfinite(scale) or scale < 1:
        raise argparse.ArgumentTypeError("Scale must be a finite number greater than or equal to 1.")
    return scale


def sharpness_value(value: str) -> float:
    try:
        amount = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Sharpen must be a number between 0 and 3.") from None
    if not math.isfinite(amount) or not 0 <= amount <= 3:
        raise argparse.ArgumentTypeError("Sharpen must be between 0 and 3.")
    return amount


def crf_value(value: str) -> int:
    try:
        crf = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("CRF must be an integer between 0 and 51.") from None
    if not 0 <= crf <= 51:
        raise argparse.ArgumentTypeError("CRF must be between 0 and 51.")
    return crf


def output_size(width: int, height: int, scale: float) -> tuple[int, int]:
    # Check before rounding to handle huge but finite scales.
    scaled_width, scaled_height = width * scale, height * scale
    if not math.isfinite(scaled_width * scaled_height) or scaled_width * scaled_height > MAX_PIXELS:
        raise ValueError("Output exceeds the 80-megapixel limit; use a smaller scale.")
    return max(1, round(scaled_width)), max(1, round(scaled_height))


def upscale_pil(image: Image.Image, scale: float, sharpen: float) -> Image.Image:
    """Core Pillow Lanczos upscale for a single frame/image.

    Returns the resized image (RGBA or RGB, optionally sharpened).
    Does NOT save to disk; callers handle I/O.
    """
    oriented = ImageOps.exif_transpose(image)
    after = output_size(*oriented.size, scale)
    alpha = "A" in oriented.getbands() or "transparency" in oriented.info
    work = oriented.convert("RGBA" if alpha else "RGB")
    result = work.resize(after, Image.Resampling.LANCZOS)
    if sharpen:
        rgb = result.convert("RGB").filter(
            ImageFilter.UnsharpMask(radius=1.5, percent=round(sharpen * 100), threshold=3)
        )
        if alpha:
            rgb.putalpha(result.getchannel("A"))
        result = rgb
    return result


def fit_to_target(image: Image.Image, target_w: int, target_h: int, sharpen: float,
                  alpha: bool = False) -> Image.Image:
    """Scale an image/frame to fit inside target_w x target_h and pad to exactly that size."""
    scale = min(target_w / image.width, target_h / image.height)
    new_w = max(1, round(image.width * scale))
    new_h = max(1, round(image.height * scale))
    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if sharpen:
        if alpha:
            rgba = resized.convert("RGBA")
            rgb = rgba.convert("RGB").filter(
                ImageFilter.UnsharpMask(radius=1.5, percent=round(sharpen * 100), threshold=3)
            )
            rgb.putalpha(rgba.getchannel("A"))
            resized = rgb
        else:
            resized = resized.filter(
                ImageFilter.UnsharpMask(radius=1.5, percent=round(sharpen * 100), threshold=3)
            )
    if alpha:
        resized = resized.convert("RGBA")
        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        canvas.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    else:
        resized = resized.convert("RGB")
        canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
        canvas.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return canvas


def upscale(source: Path, target: Path, scale: float, sharpen: float,
            quality: int, overwrite: bool, target_res: str | None = None) -> tuple[tuple[int, int], tuple[int, int]]:
    if source.resolve() == target.resolve():
        raise ValueError("Output cannot be the same as the input file.")
    if target.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {target}. Use --overwrite to replace it.")
    if target.suffix.lower() not in FORMATS:
        raise ValueError("Output extension must be PNG, JPG, WebP, BMP, or TIFF.")

    with Image.open(source) as original:
        if getattr(original, "n_frames", 1) > 1:
            raise ValueError("Animated and multi-frame images are not supported.")
        oriented = ImageOps.exif_transpose(original)
        before = oriented.size
        alpha = "A" in oriented.getbands() or "transparency" in oriented.info
        if target_res:
            tw, th = RESOLUTION_PRESETS[target_res]
            result = fit_to_target(oriented, tw, th, sharpen, alpha=alpha)
        else:
            result = upscale_pil(oriented, scale, sharpen)
        after = result.size
        alpha = result.mode == "RGBA" or "transparency" in original.info

        fmt = FORMATS[target.suffix.lower()]
        if fmt in {"JPEG", "BMP"} and alpha:
            background = Image.new("RGB", result.size, "white")
            background.paste(result, mask=result.getchannel("A"))
            result = background
        save_options = {}
        if fmt in {"JPEG", "WEBP"}:
            save_options["quality"] = quality
        if fmt == "JPEG":
            save_options["subsampling"] = 0
        if fmt in {"PNG", "JPEG", "WEBP", "TIFF"} and original.info.get("icc_profile"):
            save_options["icc_profile"] = original.info["icc_profile"]
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
            result.save(temporary, format=fmt, **save_options)
            if overwrite:
                os.replace(temporary, target)
            else:
                # Publish atomically without replacing an existing output.
                os.link(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return before, after


def _require_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            f"FFmpeg is required for video upscaling but not found: {', '.join(missing)}. "
            "Install FFmpeg and make sure it is on PATH."
        )


def _parse_fraction(value: str | None) -> float | None:
    if not value:
        return None
    try:
        num, den = value.split("/")
        fps = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return None
    return fps if math.isfinite(fps) and fps > 0 else None


def decoded_frame_size(source: Path) -> tuple[int, int] | None:
    """Return the actual decoded frame size after FFmpeg applies rotation metadata."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        with Image.open(io.BytesIO(result.stdout)) as frame:
            return frame.size
    except (OSError, UnidentifiedImageError):
        return None


def probe_video(source: Path) -> dict:
    """Probe a video file and return width, height, fps, fps_str, has_audio, duration."""
    cmd = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not run ffprobe: {error}") from None
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()[:300]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("ffprobe returned invalid JSON.") from None

    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("The input does not contain a video stream.")

    size = decoded_frame_size(source)
    if size is None:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        if not width or not height:
            raise RuntimeError("Could not determine video dimensions.")
        auto_rotate = False
    else:
        width, height = size
        auto_rotate = True

    fps_str = video.get("r_frame_rate") or ""
    fps = _parse_fraction(fps_str)
    if fps is None:
        fps_str = video.get("avg_frame_rate") or ""
        fps = _parse_fraction(fps_str)
    if fps is None:
        fps = 25.0
        fps_str = "25"

    duration_raw = video.get("duration") or data.get("format", {}).get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "fps_str": fps_str,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "duration": duration,
        "auto_rotate": auto_rotate,
    }


def _video_codec_args(suffix: str, crf: int, preset: str) -> tuple[list[str], list[str]]:
    if suffix == ".webm":
        return (
            ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0"],
            ["-c:a", "libopus", "-b:a", "128k"],
        )
    if suffix == ".avi":
        return (
            ["-c:v", "mpeg4", "-q:v", "5"],
            ["-c:a", "libmp3lame", "-b:a", "192k"],
        )
    return (
        ["-c:v", "libx264", "-crf", str(crf), "-preset", preset],
        ["-c:a", "aac", "-b:a", "192k"],
    )


def _drain(pipe, sink: list[str]) -> None:
    try:
        for line in iter(pipe.readline, b""):
            sink.append(line.decode(errors="replace"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _read_exact(stream, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = stream.read(size - len(buffer))
        if not chunk:
            break
        buffer.extend(chunk)
    return bytes(buffer)


def upscale_video(
    source: Path,
    target: Path,
    scale: float | None = None,
    target_res: str | None = None,
    sharpen: float = 0.0,
    crf: int = 23,
    preset: str = "medium",
    overwrite: bool = False,
    keep_audio: bool = True,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> tuple[tuple[int, int], tuple[int, int], int]:
    """Upscale a video by streaming raw frames through Python/Pillow and back to FFmpeg.

    With target_res the output is padded to the exact preset dimensions.
    With scale the output follows the scale factor (matching image behavior).
    """
    _require_ffmpeg()
    if source.resolve() == target.resolve():
        raise ValueError("Output cannot be the same as the input file.")
    if target.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(
            "Output video extension must be MP4, MKV, MOV, AVI, or WebM."
        )
    if target.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {target}. Use --overwrite to replace it.")

    info = probe_video(source)
    dec_w, dec_h = info["width"], info["height"]
    fps_str = info["fps_str"]
    total_frames = None
    if info.get("duration") and info.get("fps"):
        total_frames = max(1, round(info["duration"] * info["fps"]))

    if target_res:
        out_w, out_h = RESOLUTION_PRESETS[target_res]
        exact = True
    elif scale is not None:
        out_w, out_h = output_size(dec_w, dec_h, scale)
        # yuv420p requires even dimensions
        out_w += out_w % 2
        out_h += out_h % 2
        exact = False
    else:
        raise ValueError("Either scale or target_res must be provided for video upscaling.")

    if out_w * out_h > MAX_PIXELS:
        raise ValueError("Output exceeds the 80-megapixel limit; use a smaller scale or target.")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.tmp{target.suffix}")
    if temporary.exists():
        temporary.unlink()

    decoder_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if not info.get("auto_rotate", True):
        decoder_cmd += ["-autorotate", "0"]
    decoder_cmd += [
        "-i", str(source),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]

    vcodec_args, acodec_args = _video_codec_args(target.suffix.lower(), crf, preset)
    use_audio = keep_audio and info["has_audio"]
    encoder_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{out_w}x{out_h}", "-r", fps_str, "-i", "pipe:0",
    ]
    if use_audio:
        encoder_cmd += ["-i", str(source), "-map", "0:v:0", "-map", "1:a:0?"]
    else:
        encoder_cmd += ["-map", "0:v:0"]
    encoder_cmd += vcodec_args + ["-pix_fmt", "yuv420p"]
    if use_audio:
        encoder_cmd += acodec_args
    encoder_cmd.append(str(temporary))

    try:
        decoder = subprocess.Popen(
            decoder_cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise RuntimeError(f"Could not start FFmpeg decoder: {error}") from None

    try:
        encoder = subprocess.Popen(
            encoder_cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except OSError as error:
        decoder.kill()
        decoder.wait()
        raise RuntimeError(f"Could not start FFmpeg encoder: {error}") from None

    decoder_errors: list[str] = []
    encoder_errors: list[str] = []
    threads = [
        threading.Thread(target=_drain, args=(decoder.stderr, decoder_errors), daemon=True),
        threading.Thread(target=_drain, args=(encoder.stderr, encoder_errors), daemon=True),
    ]
    for thread in threads:
        thread.start()

    frame_size = dec_w * dec_h * 3
    frames = 0
    processing_ok = False
    try:
        while True:
            raw = _read_exact(decoder.stdout, frame_size)
            if not raw:
                break
            if len(raw) != frame_size:
                raise RuntimeError("FFmpeg produced a truncated frame; the video may be corrupt.")
            frame = Image.frombytes("RGB", (dec_w, dec_h), raw)
            if exact:
                output_frame = fit_to_target(frame, out_w, out_h, sharpen)
            else:
                output_frame = frame.resize((out_w, out_h), Image.Resampling.LANCZOS)
                if sharpen:
                    output_frame = output_frame.filter(
                        ImageFilter.UnsharpMask(radius=1.5, percent=round(sharpen * 100), threshold=3)
                    )
            try:
                encoder.stdin.write(output_frame.tobytes())
            except (BrokenPipeError, OSError):
                break
            frames += 1
            if progress_callback and frames % 10 == 0:
                progress_callback(frames, total_frames)
            if frames % 100 == 0:
                print(f"  {source.name}: {frames} frames processed", flush=True)
        processing_ok = True
    finally:
        try:
            encoder.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if encoder.poll() is None:
            try:
                encoder.wait(timeout=600)
            except subprocess.TimeoutExpired:
                encoder.kill()
                encoder.wait()
        if decoder.poll() is None:
            # Give the decoder a moment to exit after EOF before forcing it down.
            try:
                decoder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # The encoder stopped consuming frames; stop the decoder so it
                # cannot block forever on a full stdout pipe.
                decoder.terminate()
                try:
                    decoder.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    decoder.kill()
                    decoder.wait()
        try:
            decoder.stdout.close()
        except OSError:
            pass
        for thread in threads:
            thread.join(timeout=5)
        if not processing_ok:
            temporary.unlink(missing_ok=True)

    decoder_error_text = "".join(decoder_errors[-10:])
    encoder_error_text = "".join(encoder_errors[-10:])

    if encoder.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg encoding failed: {encoder_error_text.strip()[:500]}")
    if decoder.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg decoding failed: {decoder_error_text.strip()[:500]}")
    if frames == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("No video frames were decoded.")
    if progress_callback:
        progress_callback(frames, total_frames)

    try:
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
            temporary.unlink(missing_ok=True)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise FileExistsError(f"Output already exists: {target}. Use --overwrite to replace it.") from None
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Could not publish output {target}: {error}") from None

    return (dec_w, dec_h), (out_w, out_h), frames


class ScaleAction(argparse.Action):
    """Record that --scale was explicitly supplied while keeping its default in help."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, "_scale_explicit", True)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Upscale images and videos locally with Lanczos interpolation "
                    "(not AI super-resolution). Target exact 1080p, 2K, 4K, or 8K output.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    cli.add_argument("input", type=Path, help="Image file, video file, or directory (non-recursive)")
    cli.add_argument("-s", "--scale", action=ScaleAction, type=scale_value, default=2.0,
                     help="Scale factor, such as 2, 4, 2.5, or 8x (mutually exclusive with --target)")
    cli.add_argument("-o", "--output", type=Path,
                     help="Output file for one input, or output directory in batch mode")
    cli.add_argument("--format", choices=["png", "jpg", "webp", "bmp", "tiff"], default=None,
                     help="Image output format; video output keeps the source extension")
    cli.add_argument("--sharpen", type=sharpness_value, default=0.0,
                     help="Unsharp-mask strength from 0 to 3")
    cli.add_argument("--quality", type=int, default=95, help="JPG/WebP quality from 1 to 100")
    cli.add_argument("--overwrite", action="store_true", help="Replace existing output files")
    cli.add_argument("--target", choices=sorted(RESOLUTION_PRESETS), default=None,
                     help="Exact target resolution with aspect-preserving padding")
    cli.add_argument("--crf", type=crf_value, default=23,
                     help="Video quality CRF from 0 to 51 (lower is better)")
    cli.add_argument("--preset", choices=["ultrafast", "veryfast", "fast", "medium", "slow", "veryslow"],
                     default="medium", help="Video encoder speed/quality preset")
    cli.add_argument("--no-audio", action="store_true", help="Drop audio from video output")
    return cli


def _tag(scale: float | None, target: str | None) -> str:
    if target:
        return target
    value = 2.0 if scale is None else scale
    return f"{format(value, '.12g')}x"


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if not 1 <= args.quality <= 100:
        cli.error("--quality must be between 1 and 100.")
    if args.target and getattr(args, "_scale_explicit", False):
        cli.error("--target and --scale are mutually exclusive.")

    source = args.input
    if not source.exists():
        cli.error(f"Input not found: {source}")
    if not source.is_file() and not source.is_dir():
        cli.error("Input must be a file or directory.")

    batch = source.is_dir()
    if batch:
        supported = EXTENSIONS | VIDEO_EXTENSIONS
        sources = sorted(
            path for path in source.iterdir()
            if path.is_file() and path.suffix.lower() in supported
        )
        if not sources:
            cli.error("The directory does not contain any supported images or videos.")
        default_directory = source / "upscaled"
        output_directory = args.output or default_directory
        if output_directory.exists() and not output_directory.is_dir():
            cli.error("Batch output must be a directory.")
    else:
        sources = [source]
        output_directory = source.parent

    jobs: list[tuple[Path, Path, bool, float | None, str | None]] = []
    for item in sources:
        extension = item.suffix.lower()
        is_video = extension in VIDEO_EXTENSIONS
        is_image = extension in EXTENSIONS
        if not is_video and not is_image:
            if batch:
                continue
            cli.error(f"Unsupported input type: {item.suffix or '(no extension)'}")

        if is_video:
            target_res = args.target
            scale = None if target_res else args.scale
            tag = _tag(scale, target_res)
            name = f"{item.stem}_{tag}{extension}"
        else:
            target_res = args.target
            scale = args.scale
            tag = _tag(scale, target_res)
            image_extension = f".{args.format}" if args.format else ".png"
            name = f"{item.stem}_{tag}{image_extension}"

        if batch:
            target = output_directory / name
        elif args.output:
            if args.output.is_dir() or (not args.output.exists() and not args.output.suffix):
                target = args.output / name
            else:
                target = args.output
        else:
            target = output_directory / name

        if is_image and target.suffix.lower() not in FORMATS:
            cli.error(f"Unsupported image output extension: {target.suffix or '(none)'}")
        if is_image and args.format and FORMATS[target.suffix.lower()] != FORMATS["." + args.format]:
            cli.error("--format does not match the output file extension.")
        if is_video and target.suffix.lower() not in VIDEO_EXTENSIONS:
            cli.error(f"Unsupported video output extension: {target.suffix or '(none)'}")
        jobs.append((item, target, is_video, scale, target_res))

    target_keys = [str(target.resolve()).casefold() for _, target, _, _, _ in jobs]
    if len(target_keys) != len(set(target_keys)):
        cli.error("Output names collide; rename an input or use a different output directory.")
    input_keys = {str(item.resolve()).casefold() for item in sources}
    if any(key in input_keys for key in target_keys):
        cli.error("Output cannot overwrite an input file.")

    if batch:
        output_directory.mkdir(parents=True, exist_ok=True)

    failed = 0
    for index, (item, target, is_video, scale, target_res) in enumerate(jobs, 1):
        try:
            if is_video:
                before, after, frames = upscale_video(
                    source=item,
                    target=target,
                    scale=scale,
                    target_res=target_res,
                    sharpen=args.sharpen,
                    crf=args.crf,
                    preset=args.preset,
                    overwrite=args.overwrite,
                    keep_audio=not args.no_audio,
                )
                print(
                    f"[{index}/{len(jobs)}] OK {item.name}: "
                    f"{before[0]}x{before[1]} -> {after[0]}x{after[1]} "
                    f"({frames} frames) | {target}",
                    flush=True,
                )
            else:
                before, after = upscale(
                    item, target, scale, args.sharpen, args.quality,
                    args.overwrite, target_res=target_res,
                )
                print(
                    f"[{index}/{len(jobs)}] OK {item.name}: "
                    f"{before[0]}x{before[1]} -> {after[0]}x{after[1]} | {target}",
                    flush=True,
                )
        except (OSError, ValueError, MemoryError, RuntimeError, UnidentifiedImageError,
                Image.DecompressionBombError) as error:
            failed += 1
            print(f"[{index}/{len(jobs)}] FAILED {item.name}: {error}", file=sys.stderr, flush=True)

    print(f"Done: {len(jobs) - failed} succeeded, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
