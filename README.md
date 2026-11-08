# Image & Video Upscaler

A small, privacy-friendly tool for enlarging images and videos by 2x, 4x, or any custom scale factor, or upscaling video to exact 4K/8K resolutions. All processing happens locally with Pillow's high-quality Lanczos resampling and FFmpeg—no uploads, API keys, or external services required. Use the CLI, or drop files (or whole batches) into the bundled local web UI.

> [!NOTE]
> This is a traditional resizer, not an AI super-resolution model. It produces smooth, high-quality enlargements but does not reconstruct new details.

## Features

- Scale a single image or video, or process every supported file in a directory
- Use standard 2x and 4x scaling or any custom factor of 1 or greater
- Upscale video to exact target resolutions: 1080p, 2K, 4K, or 8K
- Preserve the original video container extension (MP4, MKV, MOV, AVI, WebM)
- Read and write PNG, JPEG, WebP, BMP, and TIFF images
- Preserve transparency in PNG, WebP, and TIFF output
- Correct EXIF orientation and preserve ICC color profiles when possible
- Apply optional sharpening after resizing
- Protect input files and existing outputs by default
- Process media locally and offline
- Run a local browser UI alongside the CLI: drag and drop single files or whole batches (up to 50), preview results inline, and download them individually or as one ZIP

## Requirements

- Python 3.10 or newer
- Pillow 12.3 or newer
- Flask 3.0 or newer (only required for the web interface)
- FFmpeg and ffprobe on `PATH` (only required for video upscaling)

## Installation

Clone the repository and create a virtual environment:

```bash
git clone https://github.com/mhazrla/enlarger.git
cd enlarger
python -m venv .venv
```

Activate the environment:

```bash
# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS or Linux
source .venv/bin/activate
```

### Option 1: install as a command (recommended)

```bash
python -m pip install -e .
```

You can now run `image-upscaler` in the examples below from the activated environment.

### Option 2: install just the dependency from `requirements.txt`

```bash
python -m pip install -r requirements.txt
```

This installs the runtime dependencies (Pillow and Flask), without installing the package itself. Run the tool with `python upscale.py` and use that in place of `image-upscaler` in the examples below. FFmpeg must still be installed separately for video upscaling.

## Usage

The default scale is 2x. Image output defaults to PNG; video output keeps the source extension:

```bash
image-upscaler photo.jpg
# Creates photo_2x.png

image-upscaler clip.mp4
# Creates clip_2x.mp4
```

Choose a 4x scale and an explicit output path:

```bash
image-upscaler photo.jpg --scale 4 --output enlarged.png
image-upscaler clip.mkv --scale 2 --output enlarged.mkv
```

Use a custom decimal scale. The optional `x` suffix is accepted:

```bash
image-upscaler photo.jpg --scale 2.5
image-upscaler photo.jpg --scale 8x --sharpen 0.6
```

Upscale a video to an exact target resolution. Video is scaled proportionally and padded with black bars to reach exactly 3840x2160:

```bash
image-upscaler clip.mp4 --target 4k
# Creates clip_4k.mp4 at exactly 3840x2160
```

Control video quality and encoding speed:

```bash
image-upscaler clip.mp4 --target 4k --crf 18 --preset slow
image-upscaler clip.mp4 --target 4k --no-audio
```

Process every supported image and video directly inside a directory:

```bash
image-upscaler ./input --target 4k --output ./output --format webp --quality 95
```

In a mixed batch, `--target` applies exact dimensions to both videos and images, padding the remaining area while preserving aspect ratio. `--scale` is used when `--target` is not set.

Without `--output`, batch results are written to an `upscaled` subdirectory. Directory processing is non-recursive.

Convert a transparent image to JPEG. Transparent areas are placed on a white background:

```bash
image-upscaler logo.png --scale 2 --output enlarged.jpg
```

See every option:

```bash
image-upscaler --help
```

### Options

| Option | Description | Default |
| --- | --- | --- |
| `input` | Image file, video file, or directory to process | Required |
| `-s`, `--scale` | Scale factor of 1 or greater; accepts values such as `2`, `2.5`, or `4x` | `2` |
| `-o`, `--output` | Output file for one input or output directory for a batch | Automatic |
| `--format` | Image output format: `png`, `jpg`, `webp`, `bmp`, or `tiff`. Video keeps its source extension | `png` |
| `--target` | Target resolution: `hd` (1920x1080), `2k` (2560x1440), `4k` (3840x2160), or `8k` (7680x4320) | Disabled |
| `--sharpen` | Unsharp-mask strength from `0` to `3` | `0` |
| `--quality` | JPEG or WebP quality from `1` to `100` | `95` |
| `--crf` | Video quality from `0` to `51`; lower is better | `23` |
| `--preset` | Video encoder speed/quality: `ultrafast` through `veryslow` | `medium` |
| `--no-audio` | Drop audio from video output | Disabled |
| `--overwrite` | Replace output files that already exist | Disabled |

## Web interface

The same pipelines are available through a local browser UI, so you can drop in a file and compare the result without typing commands.

```bash
python web.py
# or, after `pip install -e .`
image-upscaler-web
```

Then open <http://127.0.0.1:5000>.

- **Image** tab: scale or target size, sharpening, output format, and JPEG/WebP quality, with a before-and-after slider on the result.
- **Video** tab: scale or target size, sharpening, CRF, encoder preset, and a keep-audio switch, with a before/after player on the result.
- **Batches**: drop or browse several files at once (up to 50 per batch). They run one after another, each row reports its own progress, and finished files can be downloaded one by one or together as a ZIP. A batch of one keeps the before-and-after view.
- Files are uploaded to the local server, processed one at a time, and kept in `jobs/` until the job is deleted or is more than 24 hours old.
- Uploads are capped at 2 GB per file. The page reports the original and finished dimensions, the output name, and the frame count for video.
- Bind a different address or port with `python web.py --host 0.0.0.0 --port 8080`.

The HTTP API is small enough to script against: `POST /api/upscale` starts a job, `GET /api/jobs/<id>` reports progress and dimensions, and `GET /api/jobs/<id>/download` returns the finished file. For several files, `POST /api/batch` takes repeated `files` fields plus the same options, `GET /api/batch/<id>` reports per-file progress, and `GET /api/batch/<id>/download` returns a ZIP of everything that finished.

## Output behavior

- Automatic image names include the scale or target, such as `photo_2x.png` or `photo_4k.png`.
- Automatic video names include the scale or target and keep the source extension, such as `clip_2x.mp4` or `clip_4k.mkv`.
- Target output (image or video) is always exactly the preset dimensions. Aspect ratio is preserved and the remaining area is padded: black bars for video, and transparent or black padding for images.
- Explicit output file extensions determine the output format. If `--format` is also supplied, it must match that extension.
- Image dimensions are rounded to the nearest pixel after EXIF orientation is applied. Video dimensions are rounded up to even numbers for `yuv420p` compatibility.
- Only the first video stream and first audio stream are kept. Subtitles, chapters, and additional audio tracks are dropped.
- Variable-frame-rate (VFR) video is re-encoded at the source's reported frame rate, so timestamps may shift slightly.
- Output is 8-bit RGB or RGBA; HDR and 16-bit workflows are not supported.
- Animated and multi-frame images are rejected.
- Output is limited to 80 megapixels per frame to reduce accidental memory exhaustion. Large media can still require substantial RAM.
- Existing output is rejected unless `--overwrite` is used. Input files are always protected.
- Exit code `0` means success, `1` means one or more files failed, and `2` means the arguments were invalid. Batch processing continues when one file fails.

## Resource usage

Everything runs locally and offline with no GPU or model downloads. Processing is single-threaded and CPU-bound, so time spent scales with the number of output pixels. A 4x upscale produces 16x the pixels of the input.

For videos, frames are streamed from FFmpeg to Python and back, so only one frame is held in memory at a time. Video encoding adds CPU cost on top of the per-frame resize. A 4K target means every frame is resized to fit 3840x2160 and padded, so longer videos take proportionally longer. Use `--preset ultrafast` for quick drafts and a slower preset for final output.

Memory is the main cost. During a single image upscale the image is held in memory several times at once (decoded, EXIF-oriented, converted, resized, and optionally sharpened for the unsharp mask). Peak usage is therefore a few times the size of the output bitmap. An RGBA output image takes 4 bytes per pixel, so an 8000x6000 result is roughly 190 MB per in-memory copy. Because of the 80-megapixel output limit, peak memory can still exceed 1 GB on large inputs.

Batch processing handles one file at a time, so memory does not accumulate across files. To reduce peak usage, use a smaller scale or skip `--sharpen`. Output as WebP or JPEG instead of PNG to keep images smaller on disk.

## Development

Install the project, then run the test suite:

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

## License

Image & Video Upscaler CLI is available under the [MIT License](LICENSE).
