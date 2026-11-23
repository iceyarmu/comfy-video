---
name: comfy-video
description: Generate MiniMax H3 videos through ComfyUI from text, a first frame (I2VA), first and last frames (FL2VA), or reference images, videos, and audio (Ref2VA). Use when the user asks to generate an H3 video through a configured ComfyUI API.
---

# ComfyUI Video

Generate MiniMax H3 videos with the bundled ComfyUI workflows.

## Command

```bash
python3 /absolute/path/to/comfy-video/scripts/generate_video.py \
  --prompt "description with <Picture 1>" \
  --filename "output.mp4" \
  [--reference_image path1 path2] [--reference_video clip.mp4] [--reference_audio sound.wav] \
  [--first_frame path] [--last_frame path] \
  [--ratio 16:9]
```

Run from the user's working directory. Require `COMFYUI_API_URL` in the
environment; never place a server address in the script.

## Modes

All modes load `workflows/H3_Turbo_2Pass_ErosMax_b7_advanced_refmods_v9.json` when `--workflow` is omitted. To use an original workflow, pass
the corresponding option below.

| Mode | Trigger | Original workflow option |
|---|---|---|
| T2VA | Prompt only | `--workflow i2v` |
| I2VA | `--first_frame` | `--workflow i2v` |
| FL2VA | `--first_frame` and `--last_frame` | `--workflow i2v` |
| Ref2VA | Any of `--reference_image`, `--reference_video`, `--reference_audio` | `--workflow r2v` |

When providing images, the prompt must contain `<Picture 1>` through `<Picture N>` in image argument
order. Use `<Video 1>` through `<Video N>` for video references. Audio numbering
starts with video soundtracks in video order, skips silent videos, and then
continues with the standalone audio files. For example, a video with sound
and a separate WAV use `<Video 1>` and `<Audio 2>` for the video and WAV.
Reference images, videos, and audio can be combined in Ref2VA mode.
First and last frames use a separate mode. The original workflow keeps Turbo for reference images and videos without
audio tracks. If any reference video has an audio track or a standalone audio
file is supplied, it uses the base Ref2VA model and res_multistep sampler.

## Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `--prompt` | Yes | - | H3 video prompt |
| `--filename` | Yes | - | Local MP4 output path |
| `--first_frame` | By mode | - | First-frame image |
| `--last_frame` | No | - | Last-frame image; requires first frame |
| `--reference_image` | By mode | - | 1-9 ordered reference images |
| `--reference_video` | By mode | - | 1–3 ordered video clips, preferably 2–15 seconds, sampled at 24 fps |
| `--reference_audio` | By mode | - | 1–3 ordered standalone audio files |
| `--ratio` | No | `16:9` | Aspect ratio |
| `--duration` | No | `15` | Duration in seconds |
| `--megapixels` | No | v9: ~2.1, original: 0.7 | Target output megapixels |
| `--steps` | No | Workflow value | Scheduler steps |
| `--seed` | No | Workflow value | Noise seed |
| `--ref_image_size` | No | `match` | `match` or `max` |
| `--workflow` | No | `H3_Turbo_2Pass_ErosMax_b7_advanced_refmods_v9.json` | Workflow JSON path or bundled filename suffix (e.g. `i2v` or `v9`). Multiple matches use the first filename in sorted order. |

The server needs the workflow's models and custom nodes. For v9, `--steps` sets the
first pass (the second uses 3 steps). With an explicit megapixel target, both
passes generate at the requested size up to 1 MP. Above 1 MP they generate at
1 MP and RTX scales to the requested output dimensions. Omitting the option
uses the workflow default of 1 MP followed by 1.41× RTX super-resolution. Normal v9 generation uses the 2D FP16 latent
upscaler and RTX FrameGen 2× for 48 fps output.

## Behavior

- Upload inputs to ComfyUI's temporary directory and reference them with `[temp]`.
- Submit the workflow once and poll history every 10 seconds for up to 30 minutes.
  A timed-out task may still be running; use its prompt ID rather than resubmitting.
- The standard workflows combine the frames and soundtrack at 24 fps. Preview Video
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
- For video references, install `ffprobe` locally and Video Helper Suite (`VHS_LoadVideo`) on the server.
- Confirm `COMFYUI_API_URL` is set.
