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
import tempfile
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
SUPPORTED_IMAGE_EXTS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
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
    """Upload one image and return its annotated LoadImage reference."""
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in path.name)
    remote_name = f"comfy-video-{uuid.uuid4().hex[:8]}-{index + 1}-{safe_name}"
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    print(f"Uploading image {index + 1}: {path}", file=sys.stderr)
    try:
        with path.open("rb") as handle:
            response = session.post(
                f"{base_url}/upload/image",
                data={"type": "temp", "overwrite": "false"},
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
    if result.get("type") != "temp":
        fail("server did not acknowledge a temporary image upload")
    return f"{reference} [temp]"


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


def prepare_video_preview(workflow):
    for _, node in nodes_by_class(workflow, "SaveImage"):
        node.update(class_type="PreviewImage", inputs={"images": node["inputs"]["images"]},
                    _meta={"title": "Preview Image"})
    for _, node in nodes_by_class(workflow, "SaveVideo"):
        node.update(class_type="PreviewVideoTemporary", inputs={
            "video": node["inputs"]["video"], "format": "mp4", "codec": "auto",
            "include_workflow_metadata": False, "filename": "MiniMax_H3",
        }, _meta={"title": "Preview Video"})
    return one_node(workflow, "PreviewVideoTemporary")[0]


def download_video(session, base_url, entry, node_id, filename):
    # ComfyUI's video preview UI puts its file descriptor under "images".
    items = (entry.get("outputs") or {}).get(node_id, {}).get("images") or []
    if len(items) != 1 or not isinstance(items[0], dict):
        fail(f"expected one preview video from node {node_id}")
    item = items[0]
    if item.get("type") != "temp" or not (item.get("filename") or "").lower().endswith(".mp4"):
        fail(f"node {node_id} did not return a temporary MP4")
    params = {"filename": item["filename"], "subfolder": item.get("subfolder") or "", "type": "temp"}
    path = Path(filename).expanduser().resolve()
    print("Downloading preview video...", file=sys.stderr)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as directory:
            part = Path(directory) / "video.part"
            with session.get(f"{base_url}/view", params=params, stream=True, timeout=300) as response:
                response.raise_for_status()
                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        handle.write(chunk)
            if not part.stat().st_size:
                fail("server returned an empty preview video")
            os.replace(part, path)
    except (OSError, requests.RequestException) as exc:
        fail(f"video download failed: {exc}")
    return str(path)


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
    args = parser.parse_args()

    mode, image_paths = validate_args(args)
    base_url = get_base_url()
    workflow = load_workflow(mode, args.workflow)
    print(f"Mode: {mode}", file=sys.stderr)

    preview_id = prepare_video_preview(workflow)
    with requests.Session() as session:
        remote_images = [upload_image(session, base_url, path, i) for i, path in enumerate(image_paths)]
        patch_workflow(workflow, mode, remote_images, args)
        prompt_id = submit_workflow(session, base_url, workflow)
        entry = poll_history(session, base_url, prompt_id)
        saved_path = download_video(session, base_url, entry, preview_id, args.filename)
    print(saved_path)


if __name__ == "__main__":
    main()
