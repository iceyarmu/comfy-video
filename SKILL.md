---
name: comfy-video
description: Generate MiniMax H3 videos through ComfyUI from a first frame (I2VA), first and last frames (FL2VA), or 1-9 reference images (Ref2VA). Use when the user asks to submit and download an H3 video through a configured ComfyUI API. Do not use for prompt-only requests.
---

# ComfyUI Video

Generate MiniMax H3 videos with the bundled ComfyUI workflows.

## Command

```bash
python3 /absolute/path/to/comfy-video/scripts/generate_video.py \
  --prompt "description with <Picture 1>" \
  --filename "output.mp4" \
  [--reference_image path1 path2] \
  [--first_frame path] [--last_frame path] \
  [--ratio 16:9]
```

Run from the user's working directory. Require `COMFYUI_API_URL` in the
environment; never place a server address in the script.

## Modes

| Mode | Trigger | Workflow |
|---|---|---|
| I2VA | `--first_frame` | `video_minimax_h3_i2v.json` |
| FL2VA | `--first_frame` and `--last_frame` | `video_minimax_h3_i2v.json` |
| Ref2VA | `--reference_image` | `video_minimax_h3_r2v.json` |

The prompt must contain `<Picture 1>` through `<Picture N>` in image argument
order. Do not combine reference images with first or last frames.

## Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `--prompt` | Yes | - | H3 video prompt |
| `--filename` | Yes | - | Local output video path |
| `--first_frame` | By mode | - | First-frame image |
| `--last_frame` | No | - | Last-frame image; requires first frame |
| `--reference_image` | By mode | - | 1-9 ordered reference images |
| `--ratio` | No | `16:9` | Aspect ratio |
| `--duration` | No | `15` | Duration in seconds |
| `--megapixels` | No | `0.4` | Output megapixels |
| `--steps` | No | Workflow value | Scheduler steps |
| `--seed` | No | Workflow value | Noise seed |
| `--ref_image_size` | No | `match` | `match` or `max` |
| `--workflow` | No | Bundled workflow | Compatible API workflow JSON |

## Behavior

- Upload images, patch an in-memory workflow, and submit `/prompt` once.
- Poll `/history` every 10 seconds for up to 30 minutes.
- Download the returned video immediately to `--filename`.
- Print progress to stderr and the final absolute path to stdout.
- If local polling times out, report the prompt ID; do not resubmit blindly
  because the remote task may still be running.

## Preflight

- `command -v python3`
- `python3 -c "import requests"`
- Confirm `COMFYUI_API_URL` is set.

