#!/usr/bin/env python3
"""Generate MiniMax H3 videos through ComfyUI.

Requires: requests (pip install requests)
"""

import argparse
import json
import math
import mimetypes
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


SKILL_ROOT = Path(__file__).resolve().parents[1]
IMAGE_WORKFLOW = SKILL_ROOT / "workflows" / "video_minimax_h3_i2v.json"
REFERENCE_WORKFLOW = SKILL_ROOT / "workflows" / "video_minimax_h3_r2v.json"
POLL_INTERVAL = 10
TIMEOUT = 1800
CLEANUP_ENDPOINT = "/comfy_video/file"
SUPPORTED_IMAGE_EXTS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_EXTS = {".avi", ".mkv", ".mov", ".mp4", ".webm"}
ASPECT_RATIOS = {
    "1:1": "1:1 (Square)",
    "2:3": "2:3 (Portrait Photo)",
    "3:2": "3:2 (Photo)",
    "3:4": "3:4 (Portrait Standard)",
    "4:3": "4:3 (Standard)",
    "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "21:9": "21:9 (Ultrawide)",
}


class CleanupUnavailable(Exception):
    """The comfy_video_cleanup extension is not installed on the ComfyUI server."""


def fail(message):
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def get_base_url():
    value = os.environ.get("COMFYUI_API_URL", "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        fail("COMFYUI_API_URL must be set to an absolute HTTP or HTTPS base URL")
    if parsed.query or parsed.fragment:
        fail("COMFYUI_API_URL must not contain a query or fragment")
    return value


def validate_args(args):
    if args.last_frame and not args.first_frame:
        fail("--last_frame requires --first_frame")
    if args.reference_image and (args.first_frame or args.last_frame):
        fail("--reference_image cannot be combined with --first_frame/--last_frame")
    if args.reference_image and len(args.reference_image) > 9:
        fail("--reference_image supports at most 9 images")
    if not args.reference_image and not args.first_frame:
        fail("provide --first_frame or --reference_image")
    if not math.isfinite(args.duration) or args.duration <= 0:
        fail("--duration must be a finite positive number")
    if not math.isfinite(args.megapixels) or args.megapixels <= 0:
        fail("--megapixels must be a finite positive number")
    if args.steps is not None and args.steps <= 0:
        fail("--steps must be positive")

    if args.reference_image:
        mode = "ref2va"
        images = args.reference_image
    elif args.last_frame:
        mode = "fl2va"
        images = [args.first_frame, args.last_frame]
    else:
        mode = "i2va"
        images = [args.first_frame]

    image_paths = []
    for value in images:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            fail(f"image not found: {path}")
        if path.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
            fail(f"unsupported image format: {path.suffix}")
        image_paths.append(path)

    required_labels = range(1, len(image_paths) + 1)
    missing = [f"<Picture {index}>" for index in required_labels if f"<Picture {index}>" not in args.prompt]
    if missing:
        fail("prompt is missing required image labels: " + ", ".join(missing))
    return mode, image_paths


def request_json(response, action):
    try:
        response.raise_for_status()
    except requests.HTTPError:
        fail(f"{action} returned HTTP {response.status_code}: {response.text}")
    try:
        return response.json()
    except requests.JSONDecodeError:
        fail(f"{action} returned invalid JSON")


def upload_image(session, base_url, path, index):
    """Upload one image and return (workflow reference, artifact locator)."""
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in path.name)
    remote_name = f"comfy-video-{uuid.uuid4().hex[:8]}-{index + 1}-{safe_name}"
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    print(f"Uploading image {index + 1}: {path}", file=sys.stderr)
    try:
        with path.open("rb") as handle:
            response = session.post(
                f"{base_url}/upload/image",
                data={"type": "input", "overwrite": "false"},
                files={"image": (remote_name, handle, mime_type)},
                timeout=120,
            )
    except requests.RequestException as exc:
        fail(f"image upload failed: {exc}")
    result = request_json(response, "image upload")
    name = result.get("name")
    if not name:
        fail(f"unexpected upload response: {result}")
    subfolder = result.get("subfolder") or ""
    reference = f"{subfolder}/{name}" if subfolder else name
    artifact = {"filename": name, "subfolder": subfolder, "type": result.get("type") or "input"}
    return reference, artifact


def load_workflow(mode, custom_path):
    default = REFERENCE_WORKFLOW if mode == "ref2va" else IMAGE_WORKFLOW
    path = Path(custom_path).expanduser().resolve() if custom_path else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read workflow {path}: {exc}")


def nodes_by_class(workflow, class_type):
    return [(str(node_id), node) for node_id, node in workflow.items() if node.get("class_type") == class_type]


def one_node(workflow, class_type):
    nodes = nodes_by_class(workflow, class_type)
    if len(nodes) != 1:
        fail(f"expected one {class_type} node, found {len(nodes)}")
    return nodes[0]


def is_link(workflow, value):
    return isinstance(value, list) and len(value) == 2 and str(value[0]) in workflow


def next_node_id(workflow):
    numeric = [int(str(node_id)) for node_id in workflow if str(node_id).isdigit()]
    return str(max(numeric, default=0) + 1)


def set_image_input(workflow, owner, input_name, remote_name):
    current = owner.setdefault("inputs", {}).get(input_name)
    if is_link(workflow, current):
        loader = workflow[str(current[0])]
        if loader.get("class_type") == "LoadImage":
            loader.setdefault("inputs", {})["image"] = remote_name
            return str(current[0])
    node_id = next_node_id(workflow)
    workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": remote_name}}
    owner["inputs"][input_name] = [node_id, 0]
    return node_id


def set_prompt(workflow, video_node, prompt):
    current = video_node.setdefault("inputs", {}).get("prompt")
    if is_link(workflow, current):
        prompt_node = workflow[str(current[0])]
        if "value" not in prompt_node.get("inputs", {}):
            fail("linked prompt node has no value input")
        prompt_node["inputs"]["value"] = prompt
    else:
        video_node["inputs"]["prompt"] = prompt


def set_common_options(workflow, video_node, args):
    set_prompt(workflow, video_node, args.prompt)
    one_node(workflow, "PrimitiveFloat")[1].setdefault("inputs", {})["value"] = args.duration

    resolution = one_node(workflow, "ResolutionSelector")[1].setdefault("inputs", {})
    resolution["megapixels"] = args.megapixels
    resolution["aspect_ratio"] = ASPECT_RATIOS[args.ratio]

    if args.steps is not None:
        one_node(workflow, "BasicScheduler")[1].setdefault("inputs", {})["steps"] = args.steps
    if args.seed is not None:
        noise_nodes = nodes_by_class(workflow, "RandomNoise")
        if not noise_nodes:
            fail("workflow has no RandomNoise node")
        for _node_id, node in noise_nodes:
            node.setdefault("inputs", {})["noise_seed"] = args.seed

    prefix = f"video/comfy-video-{uuid.uuid4().hex[:8]}"
    one_node(workflow, "SaveVideo")[1].setdefault("inputs", {})["filename_prefix"] = prefix


def patch_workflow(workflow, mode, remote_images, args):
    if mode in {"i2va", "fl2va"}:
        video_node = one_node(workflow, "MiniMaxH3ImageToVideo")[1]
        set_image_input(workflow, video_node, "first_frame", remote_images[0])
        if mode == "fl2va":
            set_image_input(workflow, video_node, "last_frame", remote_images[1])
        else:
            video_node.setdefault("inputs", {}).pop("last_frame", None)
    else:
        video_node = one_node(workflow, "MiniMaxH3ReferenceToVideo")[1]
        inputs = video_node.setdefault("inputs", {})
        keys = sorted(
            (key for key in inputs if key.startswith("ref_images.ref_image_")),
            key=lambda key: int(key.rsplit("_", 1)[1]),
        )
        loaders = []
        for key in keys:
            link = inputs.pop(key)
            if is_link(workflow, link) and workflow[str(link[0])].get("class_type") == "LoadImage":
                loaders.append(str(link[0]))
        for index, remote_name in enumerate(remote_images):
            if index < len(loaders):
                node_id = loaders[index]
                workflow[node_id].setdefault("inputs", {})["image"] = remote_name
            else:
                node_id = next_node_id(workflow)
                workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": remote_name}}
            inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]
        inputs["ref_image_size"] = args.ref_image_size

    set_common_options(workflow, video_node, args)
    return workflow


def submit_workflow(session, base_url, workflow):
    print("Submitting generation task...", file=sys.stderr)
    try:
        response = session.post(
            f"{base_url}/prompt",
            json={"prompt": workflow, "client_id": f"comfy-video-{uuid.uuid4().hex}"},
            timeout=120,
        )
    except requests.RequestException as exc:
        fail(f"submission failed: {exc}")
    result = request_json(response, "workflow submission")
    if result.get("node_errors"):
        fail(f"workflow validation failed: {result['node_errors']}")
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        fail(f"ComfyUI did not return prompt_id: {result}")
    print(f"Task submitted: {prompt_id}", file=sys.stderr)
    return prompt_id


def poll_history(session, base_url, prompt_id):
    start = time.time()
    consecutive_errors = 0
    while True:
        elapsed = int(time.time() - start)
        if elapsed > TIMEOUT:
            fail(f"timeout after {TIMEOUT}s; remote task {prompt_id} may still be running")
        try:
            response = session.get(f"{base_url}/history/{quote(prompt_id)}", timeout=30)
            result = request_json(response, "history request")
            consecutive_errors = 0
        except requests.RequestException as exc:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                fail(f"three consecutive history failures for {prompt_id}: {exc}")
            print(f"[{elapsed}s] History error ({consecutive_errors}/3): {exc}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)
            continue

        entry = result.get(prompt_id)
        if entry:
            status = entry.get("status") or {}
            if status.get("completed"):
                if status.get("status_str") != "success":
                    fail(f"task {prompt_id} failed: {status}")
                return entry
        print(f"[{elapsed}s] Status: running", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


def select_video(entry):
    artifacts = []
    for output in (entry.get("outputs") or {}).values():
        if not isinstance(output, dict):
            continue
        for values in output.values():
            if isinstance(values, list):
                artifacts.extend(item for item in values if isinstance(item, dict) and item.get("filename"))
    if not artifacts:
        fail("ComfyUI completed successfully but returned no artifacts")
    return next((item for item in artifacts if Path(item["filename"]).suffix.lower() in VIDEO_EXTS), artifacts[0])


def artifact_params(artifact):
    """Query parameters that locate an artifact on the server."""
    return {
        "filename": artifact["filename"],
        "subfolder": artifact.get("subfolder") or "",
        "type": artifact.get("type") or "output",
    }


def download_video(session, base_url, artifact, filename):
    path = Path(filename).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    params = artifact_params(artifact)
    print("Downloading video...", file=sys.stderr)
    try:
        response = session.get(f"{base_url}/view", params=params, stream=True, timeout=300)
        response.raise_for_status()
        part_path = path.with_name(f".{path.name}.part")
        with part_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        os.replace(part_path, path)
    except (OSError, requests.RequestException) as exc:
        fail(f"video download failed: {exc}")
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"Video saved: {path} ({size_mb:.1f}MB)", file=sys.stderr)
    return str(path)


def describe_artifact(artifact):
    subfolder = artifact.get("subfolder") or ""
    name = f"{subfolder}/{artifact['filename']}" if subfolder else artifact["filename"]
    return f"{artifact.get('type') or 'output'}:{name}"


def delete_remote_file(session, base_url, artifact):
    """Delete one server-side file; return None on success or a failure reason."""
    try:
        response = session.delete(
            f"{base_url}{CLEANUP_ENDPOINT}", params=artifact_params(artifact), timeout=60
        )
    except requests.RequestException as exc:
        return str(exc)
    if response.status_code == 404:
        # The extension answers a missing file with 200 and reason "not_found",
        # so a 404 means the route itself is not registered.
        raise CleanupUnavailable
    if not response.ok:
        return f"HTTP {response.status_code}: {response.text.strip()[:200]}"
    try:
        result = response.json()
    except requests.JSONDecodeError:
        return f"unexpected response: {response.text.strip()[:200]}"
    if not result.get("deleted"):
        return result.get("reason") or result.get("error") or "server declined the deletion"
    return None


def delete_history_entry(session, base_url, prompt_id):
    """Drop the task's history record; return None on success or a reason."""
    try:
        response = session.post(f"{base_url}/history", json={"delete": [prompt_id]}, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        return str(exc)
    return None


def cleanup_remote(session, base_url, artifacts, prompt_id):
    """Best-effort removal of what this run left on the ComfyUI server.

    Never raises: the video is already on local disk by the time this runs, so a
    cleanup failure is reported as a warning rather than failing the command.
    """
    if not artifacts and not prompt_id:
        return
    print(f"Cleaning up {len(artifacts)} remote file(s)...", file=sys.stderr)
    warnings = []
    try:
        for artifact in artifacts:
            reason = delete_remote_file(session, base_url, artifact)
            if reason:
                warnings.append(f"cannot delete {describe_artifact(artifact)}: {reason}")
    except CleanupUnavailable:
        warnings.append(
            f"{base_url}{CLEANUP_ENDPOINT} is not registered, so {len(artifacts)} file(s) stay on the "
            "server; install custom_nodes/comfy_video_cleanup into ComfyUI and restart it"
        )
    if prompt_id:
        reason = delete_history_entry(session, base_url, prompt_id)
        if reason:
            warnings.append(f"cannot clear history entry {prompt_id}: {reason}")
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Generate MiniMax H3 videos through ComfyUI")
    parser.add_argument("--prompt", "-p", required=True, help="H3 video prompt")
    parser.add_argument(
        "--reference_image", nargs="+", action="extend",
        help="Reference image paths, up to 9 total; can be repeated",
    )
    parser.add_argument("--first_frame", help="First-frame image path")
    parser.add_argument("--last_frame", help="Last-frame image path; requires --first_frame")
    parser.add_argument("--ratio", choices=ASPECT_RATIOS, default="16:9")
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--megapixels", type=float, default=0.7)
    parser.add_argument("--steps", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--ref_image_size", choices=["match", "max"], default="match")
    parser.add_argument("--workflow", help="Compatible API-format workflow JSON")
    parser.add_argument("--filename", "-f", required=True, help="Output video path")
    parser.add_argument(
        "--keep_remote",
        action="store_true",
        help="Keep the uploaded images and rendered video on the ComfyUI server",
    )
    args = parser.parse_args()

    mode, image_paths = validate_args(args)
    base_url = get_base_url()
    workflow = load_workflow(mode, args.workflow)
    print(f"Mode: {mode}", file=sys.stderr)

    with requests.Session() as session:
        remote_images = []
        remote_artifacts = []
        prompt_id = None
        finished = False
        try:
            for index, path in enumerate(image_paths):
                reference, artifact = upload_image(session, base_url, path, index)
                remote_images.append(reference)
                remote_artifacts.append(artifact)
            patch_workflow(workflow, mode, remote_images, args)
            prompt_id = submit_workflow(session, base_url, workflow)
            entry = poll_history(session, base_url, prompt_id)
            video_artifact = select_video(entry)
            saved_path = download_video(session, base_url, video_artifact, args.filename)
            # Queue the video for deletion only once it is safely on local disk.
            remote_artifacts.append(video_artifact)
            finished = True
        finally:
            if args.keep_remote:
                pass
            elif prompt_id is not None and not finished:
                # The task may still be running and reading the uploaded images.
                print(f"Warning: remote files kept; task {prompt_id} did not finish cleanly", file=sys.stderr)
            else:
                cleanup_remote(session, base_url, remote_artifacts, prompt_id)
    print(saved_path)


if __name__ == "__main__":
    main()
