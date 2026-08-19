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
  [--ratio 16:9] [--keep_remote]
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
| `--megapixels` | No | `0.7` | Output megapixels |
| `--steps` | No | Workflow value | Scheduler steps |
| `--seed` | No | Workflow value | Noise seed |
| `--ref_image_size` | No | `match` | `match` or `max` |
| `--workflow` | No | Bundled workflow | Compatible API workflow JSON |
| `--keep_remote` | No | Off | Leave the uploaded images and rendered video on the server |

## Behavior

- Upload images, patch an in-memory workflow, and submit `/prompt` once.
- Poll `/history` every 10 seconds for up to 30 minutes.
- Download the returned video immediately to `--filename`.
- Delete the uploaded images and the rendered video from the server, then drop
  the history entry. See Cleanup.
- Print progress to stderr and the final absolute path to stdout.
- If local polling times out, report the prompt ID; do not resubmit blindly
  because the remote task may still be running.

## Cleanup

ComfyUI's own API has no route for deleting files from `input/` or `output/`;
`POST /history` only drops the in-memory record. Cleanup therefore needs a
server-side extension.

Copy `custom_nodes/comfy_video_cleanup/` into the ComfyUI server's
`custom_nodes/` directory and restart ComfyUI. It registers a single route,
`DELETE /comfy_video/file`, taking the same `filename`/`subfolder`/`type` query
parameters as `/view`. It refuses any path that escapes the target directory and
any file whose name does not start with `comfy-video-`, which is the prefix the
script gives every upload and every rendered video.

Cleanup is best effort: the video is already on local disk when it runs, so
failures print a warning and do not change the exit status. If the extension is
not installed the endpoint returns 404 and the script says so once.

Files are left on the server when:

- `--keep_remote` is passed.
- The task was submitted but did not finish cleanly, since it may still be
  running and reading the uploaded images. The warning names the prompt ID.

The rendered video is only deleted after it lands on local disk, so a failed
download leaves it retrievable on the server.

## Preflight

- `command -v python3`
- `python3 -c "import requests"`
- Confirm `COMFYUI_API_URL` is set.
- Optional: `curl -so /dev/null -w '%{http_code}' -X DELETE
  "$COMFYUI_API_URL/comfy_video/file"` returns `400` when the cleanup extension
  is installed and `404` when it is not.
