---
name: comfy-video
description: Generate MiniMax H3 videos through ComfyUI from a first frame (I2VA), first and last frames (FL2VA), or 1-9 reference images (Ref2VA). Use when the user asks to generate an H3 video through a configured ComfyUI API. Do not use for prompt-only requests.
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
| `--filename` | Yes | - | Local MP4 output path |
| `--first_frame` | By mode | - | First-frame image |
| `--last_frame` | No | - | Last-frame image; requires first frame |
| `--reference_image` | By mode | - | 1-9 ordered reference images |
| `--ratio` | No | `16:9` | Aspect ratio |
| `--duration` | No | `15` | Duration in seconds |
| `--megapixels` | No | `0.7` | Output megapixels |
| `--steps` | No | Workflow value | Scheduler steps |
| `--seed` | No | Workflow value | Noise seed |
| `--ref_image_size` | No | `match` | `match` or `max` |
| `--workflow` | No | Bundled workflow | Compatible API workflow JSON path or bundled ErosMax v9 name |

Use `--workflow H3_Turbo_2Pass_ErosMax_b7_advanced_refmods_v9` for
ErosMax two-pass generation with any of the three image modes. The server
needs the workflow's models and custom nodes. For v9, `--steps` sets the
first pass (the second uses 3 steps), and `--megapixels` sets the second-pass
target before RTX upscaling.

## Behavior

- Upload inputs to ComfyUI's temporary directory and reference them with `[temp]`.
- Submit the workflow once and poll history every 10 seconds for up to 30 minutes.
  A timed-out task may still be running; use its prompt ID rather than resubmitting.
- Create Video combines the frames and soundtrack at 24 fps. Preview Video
  returns a temporary MP4; download it directly to `--filename`.
- No generated file goes to the server's output directory. Temporary files and
  job history follow the server's retention policy; no deletion API is needed.
- A compatible custom workflow must have one Preview Video output configured
  for MP4, or one legacy SaveVideo output. The script converts SaveVideo to
  Preview Video before submission, preserving the incoming video connection.
- Progress goes to stderr and the final absolute path to stdout.

## Preflight

- `command -v python3`
- `python3 -c "import requests"`
- The server must have [Preview Video](https://github.com/elgalardi/ComfyUI-PreviewVideo)
  installed (node ID: `PreviewVideoTemporary`).
- Confirm `COMFYUI_API_URL` is set.
