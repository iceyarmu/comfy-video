---
name: comfy-video
description: Generate MiniMax H3 videos through a ComfyUI API from a first frame (I2VA), first and last frames (FL2VA), or 1-9 ordered reference images (Ref2VA). Use when Codex needs to check ComfyUI compatibility, upload image inputs, patch and submit the bundled API workflows, monitor a generation job in the background, recover its local status, or download the resulting video. Do not use for prompt-only requests.
---

# ComfyUI Video

Generate MiniMax H3 video through ComfyUI with the bundled Python runtime and
API-format workflows. Keep the runtime in charge of uploads, submission,
monitoring, and downloads.

## Configuration

Require `COMFYUI_API_URL` in the process environment. Treat it as the ComfyUI
base URL, without an API endpoint suffix. Never add a server address to the
script or skill files.

The runtime uses only the Python standard library. Run it from the user's
working directory so `.comfyui-jobs/` and default outputs belong to that task,
not to the skill directory.

```bash
export COMFYUI_API_URL="<comfyui-base-url>"
python3 /absolute/path/to/comfy-video/scripts/generate_video.py check
```

`check` is read-only. It validates the bundled workflow structure and the
server's required H3 node interfaces without submitting a GPU job.

## Generation Workflow

1. Confirm that the user explicitly requested video generation. Submission
   uploads files and consumes remote GPU resources.
2. Select the mode and preserve image order:
   - Use `i2va` for one first frame.
   - Use `fl2va` for a first frame followed by a last frame.
   - Use `ref2va` for 1-9 ordered reference images.
3. Prepare a prompt containing each required `<Picture N>` label. The runtime
   rejects missing labels locally before upload or submission.
4. Run `check` when the server, custom nodes, models, or workflow templates may
   have changed.
5. Run `generate` without `--foreground`. Capture the returned `job_id` and
   `job_dir` from its JSON output.
6. Use one `wait --job <job-id>` process when waiting in the same turn. Use
   `status --job <job-id>` when resuming or when the user asks for status.
7. On completion, return the absolute paths under `outputs` in `status.json`.
   On failure, report `error` and inspect `worker.log` before deciding what to
   do next.

## Commands

### I2VA

```bash
python3 /absolute/path/to/comfy-video/scripts/generate_video.py generate \
  --mode i2va \
  --prompt-file /absolute/path/to/prompt.txt \
  --first-image /absolute/path/to/first.png \
  --duration 15 \
  --megapixels 0.4 \
  --output-dir /absolute/path/to/output
```

### FL2VA

```bash
python3 /absolute/path/to/comfy-video/scripts/generate_video.py generate \
  --mode fl2va \
  --prompt-file /absolute/path/to/prompt.txt \
  --first-image /absolute/path/to/first.png \
  --last-image /absolute/path/to/last.png \
  --duration 15 \
  --megapixels 0.4 \
  --output-dir /absolute/path/to/output
```

### Ref2VA

Repeat `--reference-image` in the exact order used by `<Picture N>` labels.

```bash
python3 /absolute/path/to/comfy-video/scripts/generate_video.py generate \
  --mode ref2va \
  --prompt-file /absolute/path/to/prompt.txt \
  --reference-image /absolute/path/to/subject.png \
  --reference-image /absolute/path/to/scene.png \
  --duration 15 \
  --megapixels 0.4 \
  --ref-image-size match \
  --output-dir /absolute/path/to/output
```

### Status And Wait

```bash
python3 /absolute/path/to/comfy-video/scripts/generate_video.py status \
  --job <job-id>

python3 /absolute/path/to/comfy-video/scripts/generate_video.py wait \
  --job <job-id>
```

## Runtime Behavior

- Use `workflows/video_minimax_h3_i2v.json` for I2VA and FL2VA.
- Use `workflows/video_minimax_h3_r2v.json` for Ref2VA.
- Use `--workflow` to supply a compatible API-format workflow for one
  generation command.
- Default to 15 seconds and 0.4 megapixels. Preserve user-supplied duration,
  megapixels, aspect ratio, scheduler steps, seed, and reference sizing.
- Open WebSocket before submission, upload each input with a unique name,
  patch an in-memory workflow copy, and POST `/prompt` exactly once.
- Let the background worker own WebSocket events and `/history` fallback.
- Download all returned artifacts through `/view` and record absolute path,
  byte size, and SHA-256 in `status.json`.
- Store each job under `.comfyui-jobs/<job-id>/` with `spec.json`,
  `status.json`, and `worker.log`.

## Safety Boundaries

- Do not submit from a prompt-only request.
- Do not submit the same job again because a terminal wait or model turn timed
  out. Read the existing job's `status.json` first.
- A local `timed_out` state does not prove that the remote GPU job stopped.
  Inspect the remote queue or history before taking further action.
- Do not cancel or interrupt a remote job without explicit user authorization.
- Do not combine `--reference-image` with first/last-frame modes.
- Keep FL2VA input order as first frame, then last frame. Keep Ref2VA inputs in
  prompt-label order.
- Use `--foreground` only when the user explicitly asks for a foreground run.

