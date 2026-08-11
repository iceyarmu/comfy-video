#!/usr/bin/env python3
"""Submit and monitor MiniMax H3 ComfyUI jobs without model-driven polling.

The ComfyUI server address is read from the COMFYUI_API_URL environment
variable. No server-specific URL is embedded in this runtime.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid


API_URL_ENV = "COMFYUI_API_URL"
SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = SKILL_ROOT / "workflows"
DEFAULT_IMAGE_WORKFLOW = WORKFLOW_DIR / "video_minimax_h3_i2v.json"
DEFAULT_REFERENCE_WORKFLOW = WORKFLOW_DIR / "video_minimax_h3_r2v.json"
TERMINAL_STATES = {"completed", "failed", "timed_out"}
ASPECT_RATIO_ALIASES = {
    "1:1": "1:1 (Square)",
    "2:3": "2:3 (Portrait Photo)",
    "3:2": "3:2 (Photo)",
    "3:4": "3:4 (Portrait Standard)",
    "4:3": "4:3 (Standard)",
    "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "21:9": "21:9 (Ultrawide)",
}


class UserError(Exception):
    pass


def get_api_url() -> str:
    raw_value = os.environ.get(API_URL_ENV)
    if not raw_value or not raw_value.strip():
        raise UserError(f"Required environment variable is not set: {API_URL_ENV}")
    api_url = raw_value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UserError(
            f"{API_URL_ENV} must be an absolute HTTP or HTTPS base URL"
        )
    if parsed.query or parsed.fragment:
        raise UserError(f"{API_URL_ENV} must not contain a query or fragment")
    return api_url


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_aspect_ratio(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return ASPECT_RATIO_ALIASES.get(stripped, stripped)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UserError(f"Cannot read JSON from {path}: {exc}") from exc


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp_path, path)


def update_status(status_path: Path, state: str, **fields: object) -> dict:
    status = read_json(status_path) if status_path.exists() else {}
    status.update(fields)
    status["state"] = state
    status["updated_at"] = utc_now()
    write_json_atomic(status_path, status)
    return status


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ComfyApi:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> bytes:
        request = urllib.request.Request(
            self._url(path, query), data=body, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise UserError(
                f"ComfyUI {method} {path} returned HTTP {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise UserError(f"ComfyUI {method} {path} failed: {exc}") from exc

    def json(
        self,
        method: str,
        path: str,
        value: dict | None = None,
        *,
        timeout: float = 60,
    ) -> dict:
        body = None
        headers = {}
        if value is not None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        payload = self.request(method, path, body=body, headers=headers, timeout=timeout)
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise UserError(f"ComfyUI {path} returned invalid JSON") from exc

    def upload_image(self, image_path: Path, remote_name: str) -> str:
        boundary = f"----comfy-video-{secrets.token_hex(16)}"
        content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        parts: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode(),
                    b"\r\n",
                ]
            )

        add_field("type", "input")
        add_field("overwrite", "false")
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="image"; '
                    f'filename="{remote_name}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                image_path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        response = self.request(
            "POST",
            "/upload/image",
            body=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=120,
        )
        result = json.loads(response.decode("utf-8"))
        name = result.get("name")
        if not name:
            raise UserError(f"Unexpected upload response: {result}")
        subfolder = result.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    def download_artifact(self, artifact: dict, output_path: Path) -> None:
        query = {
            "filename": str(artifact["filename"]),
            "subfolder": str(artifact.get("subfolder") or ""),
            "type": str(artifact.get("type") or "output"),
        }
        payload = self.request("GET", "/view", query=query, timeout=300)
        part_path = output_path.with_name(f".{output_path.name}.part")
        part_path.write_bytes(payload)
        os.replace(part_path, output_path)


class WebSocketClient:
    """Small RFC 6455 client sufficient for ComfyUI event streams."""

    def __init__(self, api_url: str, client_id: str, timeout: float = 15):
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme not in {"http", "https"}:
            raise UserError(f"Unsupported ComfyUI URL scheme: {parsed.scheme}")
        host = parsed.hostname
        if not host:
            raise UserError(f"Invalid ComfyUI URL: {api_url}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        raw_socket = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            self.sock = context.wrap_socket(raw_socket, server_hostname=host)
        else:
            self.sock = raw_socket
        self.buffer = bytearray()
        self.fragments = bytearray()
        self.fragment_opcode: int | None = None
        base_path = parsed.path.rstrip("/")
        ws_path = f"{base_path}/ws" if base_path else "/ws"
        ws_path += "?" + urllib.parse.urlencode({"clientId": client_id})
        host_header = host if port in {80, 443} else f"{host}:{port}"
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            f"GET {ws_path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        self.sock.sendall(request)
        response = self._read_headers()
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            self.close()
            raise UserError(f"WebSocket handshake failed: {status_line.decode(errors='replace')}")
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        headers = {}
        for line in response.split(b"\r\n")[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.decode().lower()] = value.decode().strip()
        if headers.get("sec-websocket-accept") != expected:
            self.close()
            raise UserError("WebSocket server returned an invalid accept key")

    def _read_headers(self) -> bytes:
        marker = b"\r\n\r\n"
        while marker not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise UserError("WebSocket closed during handshake")
            self.buffer.extend(chunk)
        header_end = self.buffer.index(marker) + len(marker)
        headers = bytes(self.buffer[:header_end])
        del self.buffer[:header_end]
        return headers

    def _recv_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise EOFError("WebSocket connection closed")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        length = len(masked)
        if length <= 125:
            header = bytes([0x80 | opcode, 0x80 | length])
        else:
            raise UserError("WebSocket control frame is too large")
        self.sock.sendall(header + mask + masked)

    def receive_json(self, timeout: float) -> dict | None:
        self.sock.settimeout(timeout)
        while True:
            header = self._recv_exact(2)
            first, second = header
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA or opcode == 0x2:
                continue
            if opcode in {0x1, 0x0}:
                if opcode == 0x1 and not final:
                    self.fragment_opcode = opcode
                    self.fragments = bytearray(payload)
                    continue
                if opcode == 0x0:
                    self.fragments.extend(payload)
                    if not final:
                        continue
                    payload = bytes(self.fragments)
                    self.fragments.clear()
                    self.fragment_opcode = None
                try:
                    return json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def nodes_by_class(workflow: dict, class_type: str) -> list[tuple[str, dict]]:
    return [
        (str(node_id), node)
        for node_id, node in workflow.items()
        if node.get("class_type") == class_type
    ]


def one_node(workflow: dict, class_type: str) -> tuple[str, dict]:
    matches = nodes_by_class(workflow, class_type)
    if len(matches) != 1:
        raise UserError(f"Expected one {class_type} node, found {len(matches)}")
    return matches[0]


def is_link(value: object, workflow: dict) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and str(value[0]) in workflow
        and isinstance(value[1], int)
    )


def linked_nodes(workflow: dict, start: object) -> list[tuple[str, dict]]:
    if not is_link(start, workflow):
        return []
    queue = [str(start[0])]
    seen: set[str] = set()
    result: list[tuple[str, dict]] = []
    while queue:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        node = workflow[node_id]
        result.append((node_id, node))
        for value in node.get("inputs", {}).values():
            if is_link(value, workflow):
                queue.append(str(value[0]))
    return result


def next_node_id(workflow: dict) -> str:
    numeric = [int(str(node_id)) for node_id in workflow if str(node_id).isdigit()]
    candidate = max(numeric, default=0) + 1
    while str(candidate) in workflow:
        candidate += 1
    return str(candidate)


def ensure_load_image(workflow: dict, owner: dict, input_name: str, image: str) -> str:
    existing = owner.get("inputs", {}).get(input_name)
    if is_link(existing, workflow):
        node_id = str(existing[0])
        node = workflow[node_id]
        if node.get("class_type") == "LoadImage":
            node.setdefault("inputs", {})["image"] = image
            return node_id
    node_id = next_node_id(workflow)
    workflow[node_id] = {
        "class_type": "LoadImage",
        "inputs": {"image": image},
        "_meta": {"title": f"Load {input_name}"},
    }
    owner.setdefault("inputs", {})[input_name] = [node_id, 0]
    return node_id


def set_prompt(workflow: dict, video_node: dict, prompt: str) -> None:
    current = video_node.get("inputs", {}).get("prompt")
    if is_link(current, workflow):
        prompt_node = workflow[str(current[0])]
        if "value" not in prompt_node.get("inputs", {}):
            raise UserError("Linked prompt node has no value input")
        prompt_node["inputs"]["value"] = prompt
    else:
        video_node.setdefault("inputs", {})["prompt"] = prompt


def set_duration(workflow: dict, video_node: dict, seconds: float) -> None:
    length = video_node.get("inputs", {}).get("length")
    if is_link(length, workflow):
        for _node_id, node in linked_nodes(workflow, length):
            if node.get("class_type") == "PrimitiveFloat":
                node.setdefault("inputs", {})["value"] = seconds
                return
    frames = max(5, round(seconds * 24))
    frames += (5 - (frames % 17)) % 17
    video_node.setdefault("inputs", {})["length"] = frames


def set_resolution(
    workflow: dict, video_node: dict, megapixels: float, aspect_ratio: str | None
) -> None:
    candidates: dict[str, dict] = {}
    for input_name in ("width", "height"):
        for node_id, node in linked_nodes(
            workflow, video_node.get("inputs", {}).get(input_name)
        ):
            if node.get("class_type") == "ResolutionSelector":
                candidates[node_id] = node
    if not candidates:
        for node_id, node in nodes_by_class(workflow, "ResolutionSelector"):
            candidates[node_id] = node
    if len(candidates) != 1:
        raise UserError(f"Expected one ResolutionSelector, found {len(candidates)}")
    node = next(iter(candidates.values()))
    node.setdefault("inputs", {})["megapixels"] = megapixels
    if aspect_ratio:
        node["inputs"]["aspect_ratio"] = normalize_aspect_ratio(aspect_ratio)


def set_steps(workflow: dict, steps: int) -> None:
    _node_id, scheduler = one_node(workflow, "BasicScheduler")
    scheduler.setdefault("inputs", {})["steps"] = steps


def set_common_options(workflow: dict, video_node: dict, spec: dict) -> None:
    set_duration(workflow, video_node, float(spec["duration"]))
    set_resolution(
        workflow, video_node, float(spec["megapixels"]), spec.get("aspect_ratio")
    )
    if spec.get("steps") is not None:
        set_steps(workflow, int(spec["steps"]))
    _save_id, save_node = one_node(workflow, "SaveVideo")
    save_node.setdefault("inputs", {})["filename_prefix"] = spec["output_prefix"]
    if spec.get("seed") is not None:
        random_nodes = nodes_by_class(workflow, "RandomNoise")
        if not random_nodes:
            raise UserError("Seed was provided but the workflow has no RandomNoise node")
        for _node_id, node in random_nodes:
            node.setdefault("inputs", {})["noise_seed"] = int(spec["seed"])


def patch_fl2va(workflow: dict, spec: dict, remote_images: list[str]) -> None:
    _node_id, video_node = one_node(workflow, "MiniMaxH3ImageToVideo")
    set_prompt(workflow, video_node, spec["prompt"])
    ensure_load_image(workflow, video_node, "first_frame", remote_images[0])
    ensure_load_image(workflow, video_node, "last_frame", remote_images[1])
    set_common_options(workflow, video_node, spec)


def referenced_node_ids(workflow: dict) -> set[str]:
    result: set[str] = set()
    for node in workflow.values():
        for value in node.get("inputs", {}).values():
            if is_link(value, workflow):
                result.add(str(value[0]))
    return result


def patch_i2va(workflow: dict, spec: dict, remote_images: list[str]) -> None:
    _node_id, video_node = one_node(workflow, "MiniMaxH3ImageToVideo")
    set_prompt(workflow, video_node, spec["prompt"])
    inputs = video_node.setdefault("inputs", {})
    old_last_frame = inputs.pop("last_frame", None)
    ensure_load_image(workflow, video_node, "first_frame", remote_images[0])
    if is_link(old_last_frame, workflow):
        old_loader_id = str(old_last_frame[0])
        if old_loader_id not in referenced_node_ids(workflow):
            workflow.pop(old_loader_id, None)
    set_common_options(workflow, video_node, spec)


def patch_ref2va(workflow: dict, spec: dict, remote_images: list[str]) -> None:
    _node_id, video_node = one_node(workflow, "MiniMaxH3ReferenceToVideo")
    set_prompt(workflow, video_node, spec["prompt"])
    inputs = video_node.setdefault("inputs", {})
    existing_loaders: list[str] = []
    for key in sorted(list(inputs)):
        if key.startswith("ref_images.ref_image_"):
            link = inputs.pop(key)
            if is_link(link, workflow):
                existing_loaders.append(str(link[0]))
    used_loaders: list[str] = []
    for index, image in enumerate(remote_images):
        if index < len(existing_loaders):
            node_id = existing_loaders[index]
            node = workflow[node_id]
            if node.get("class_type") != "LoadImage":
                raise UserError(f"Reference input {index} is not linked to LoadImage")
            node.setdefault("inputs", {})["image"] = image
        else:
            node_id = next_node_id(workflow)
            workflow[node_id] = {
                "class_type": "LoadImage",
                "inputs": {"image": image},
                "_meta": {"title": f"Reference Picture {index + 1}"},
            }
        inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]
        used_loaders.append(node_id)
    inputs["ref_image_size"] = spec["ref_image_size"]
    still_referenced = referenced_node_ids(workflow)
    for node_id in existing_loaders:
        if node_id not in used_loaders and node_id not in still_referenced:
            workflow.pop(node_id, None)
    set_common_options(workflow, video_node, spec)


def validate_prompt_labels(mode: str, prompt: str, image_count: int) -> None:
    if mode == "i2va":
        required = range(1, 2)
    elif mode == "fl2va":
        required = range(1, 3)
    else:
        required = range(1, image_count + 1)
    missing = [f"<Picture {index}>" for index in required if f"<Picture {index}>" not in prompt]
    if missing:
        raise UserError("Prompt is missing required image labels: " + ", ".join(missing))


def sanitize_remote_name(job_id: str, index: int, path: Path) -> str:
    safe_name = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in path.name
    )
    return f"{job_id}_{index + 1}_{safe_name}"


def collect_artifacts(entry: dict) -> list[dict]:
    artifacts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for node_output in (entry.get("outputs") or {}).values():
        if not isinstance(node_output, dict):
            continue
        for value in node_output.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or "filename" not in item:
                    continue
                key = (
                    str(item["filename"]),
                    str(item.get("subfolder") or ""),
                    str(item.get("type") or "output"),
                )
                if key not in seen:
                    seen.add(key)
                    artifacts.append(item)
    return artifacts


def unique_output_path(output_dir: Path, filename: str, job_id: str) -> Path:
    safe_name = Path(filename).name
    candidate = output_dir / safe_name
    if not candidate.exists():
        return candidate
    return output_dir / f"{candidate.stem}-{job_id}{candidate.suffix}"


def history_entry(api: ComfyApi, prompt_id: str) -> dict | None:
    history = api.json("GET", f"/history/{urllib.parse.quote(prompt_id)}", timeout=30)
    return history.get(prompt_id)


def completed_successfully(entry: dict) -> bool:
    status = entry.get("status") or {}
    return bool(status.get("completed")) and status.get("status_str") == "success"


def failure_from_history(entry: dict) -> str | None:
    status = entry.get("status") or {}
    if status.get("completed") and status.get("status_str") != "success":
        return json.dumps(status, ensure_ascii=False)
    for message in status.get("messages") or []:
        if isinstance(message, list) and message and message[0] in {
            "execution_error",
            "execution_interrupted",
        }:
            return json.dumps(message, ensure_ascii=False)
    return None


def download_outputs(
    api: ComfyApi, entry: dict, output_dir: Path, job_id: str
) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = collect_artifacts(entry)
    if not artifacts:
        raise UserError("ComfyUI completed successfully but returned no output artifacts")
    downloaded = []
    for artifact in artifacts:
        output_path = unique_output_path(output_dir, artifact["filename"], job_id)
        api.download_artifact(artifact, output_path)
        downloaded.append(
            {
                "path": str(output_path.resolve()),
                "size": output_path.stat().st_size,
                "sha256": sha256_file(output_path),
                "remote": {
                    "filename": artifact["filename"],
                    "subfolder": artifact.get("subfolder") or "",
                    "type": artifact.get("type") or "output",
                },
            }
        )
    return downloaded


def monitor_job(
    api: ComfyApi,
    websocket: WebSocketClient | None,
    prompt_id: str,
    spec: dict,
    status_path: Path,
) -> dict:
    poll_interval = float(spec["poll_interval"])
    timeout = float(spec.get("timeout") or 0)
    started = time.monotonic()
    last_history_check = 0.0
    while True:
        if timeout and time.monotonic() - started > timeout:
            return update_status(
                status_path,
                "timed_out",
                error="Local monitor timed out; the ComfyUI job may still be running",
            )
        event = None
        if websocket is not None:
            try:
                event = websocket.receive_json(timeout=poll_interval)
                if event is None:
                    websocket.close()
                    websocket = None
            except socket.timeout:
                pass
            except (EOFError, OSError, UserError) as exc:
                print(f"WebSocket unavailable, falling back to history polling: {exc}", flush=True)
                websocket.close()
                websocket = None
        else:
            time.sleep(min(poll_interval, 5.0))

        now = time.monotonic()
        if event:
            event_type = event.get("type")
            data = event.get("data") or {}
            if data.get("prompt_id") == prompt_id:
                fields: dict[str, object] = {"last_event": event_type}
                if event_type == "progress":
                    fields["progress"] = {
                        "value": data.get("value"),
                        "max": data.get("max"),
                        "node": data.get("node"),
                    }
                elif event_type == "executing":
                    fields["current_node"] = data.get("node")
                update_status(status_path, "running", **fields)
                if event_type in {"execution_error", "execution_interrupted"}:
                    return update_status(
                        status_path,
                        "failed",
                        error=json.dumps(event, ensure_ascii=False),
                    )
                if event_type in {"execution_success", "executed"}:
                    last_history_check = 0.0

        if now - last_history_check < poll_interval and event is not None:
            continue
        last_history_check = now
        entry = history_entry(api, prompt_id)
        if not entry:
            continue
        failure = failure_from_history(entry)
        if failure:
            return update_status(status_path, "failed", error=failure)
        if completed_successfully(entry):
            outputs = download_outputs(
                api, entry, Path(spec["output_dir"]), spec["job_id"]
            )
            return update_status(
                status_path,
                "completed",
                completed_at=utc_now(),
                outputs=outputs,
                history_status=entry.get("status"),
            )


def execute_spec(spec_path: Path) -> dict:
    spec = read_json(spec_path)
    status_path = spec_path.parent / "status.json"
    api = ComfyApi(spec["api_url"])
    websocket: WebSocketClient | None = None
    try:
        update_status(status_path, "starting", started_at=utc_now())
        workflow = read_json(Path(spec["workflow"]))
        image_paths = [Path(path) for path in spec["images"]]
        validate_prompt_labels(spec["mode"], spec["prompt"], len(image_paths))

        client_id = f"comfy-video-{spec['job_id']}-{uuid.uuid4().hex[:8]}"
        try:
            websocket = WebSocketClient(spec["api_url"], client_id)
            update_status(status_path, "uploading", client_id=client_id, websocket=True)
        except (OSError, UserError) as exc:
            print(f"WebSocket connection failed; history polling will be used: {exc}", flush=True)
            update_status(status_path, "uploading", client_id=client_id, websocket=False)
            websocket = None

        remote_images = []
        for index, image_path in enumerate(image_paths):
            remote_images.append(
                api.upload_image(
                    image_path,
                    sanitize_remote_name(spec["job_id"], index, image_path),
                )
            )
        if spec["mode"] == "i2va":
            patch_i2va(workflow, spec, remote_images)
            node_class = "MiniMaxH3ImageToVideo"
        elif spec["mode"] == "fl2va":
            patch_fl2va(workflow, spec, remote_images)
            node_class = "MiniMaxH3ImageToVideo"
        else:
            patch_ref2va(workflow, spec, remote_images)
            node_class = "MiniMaxH3ReferenceToVideo"

        node_info = api.json(
            "GET", f"/object_info/{urllib.parse.quote(node_class)}", timeout=30
        )
        if node_class not in node_info:
            raise UserError(f"Remote ComfyUI does not provide {node_class}")
        if spec["mode"] in {"i2va", "fl2va"}:
            optional = node_info[node_class].get("input", {}).get("optional", {})
            if "first_frame" not in optional:
                raise UserError("Remote MiniMaxH3ImageToVideo lacks first_frame support")
        if spec["mode"] == "fl2va":
            if "last_frame" not in optional:
                raise UserError("Remote MiniMaxH3ImageToVideo lacks last_frame support")

        update_status(status_path, "submitting", uploaded_images=remote_images)
        result = api.json(
            "POST", "/prompt", {"prompt": workflow, "client_id": client_id}, timeout=120
        )
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise UserError(f"ComfyUI did not return prompt_id: {result}")
        node_errors = result.get("node_errors") or {}
        if node_errors:
            raise UserError(f"ComfyUI workflow validation failed: {node_errors}")
        update_status(
            status_path,
            "running",
            prompt_id=prompt_id,
            queue_number=result.get("number"),
            submitted_at=utc_now(),
        )
        return monitor_job(api, websocket, prompt_id, spec, status_path)
    except Exception as exc:
        print(traceback.format_exc(), flush=True)
        return update_status(status_path, "failed", error=str(exc), failed_at=utc_now())
    finally:
        if websocket is not None:
            websocket.close()


def prompt_from_args(args: argparse.Namespace) -> str:
    if bool(args.prompt) == bool(args.prompt_file):
        raise UserError("Provide exactly one of --prompt or --prompt-file")
    if args.prompt:
        prompt = args.prompt
    else:
        path = Path(args.prompt_file).expanduser().resolve()
        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise UserError(f"Cannot read prompt file {path}: {exc}") from exc
    if not prompt.strip():
        raise UserError("Prompt must not be empty")
    return prompt


def build_spec(
    args: argparse.Namespace, job_id: str, workspace: Path, api_url: str
) -> dict:
    prompt = prompt_from_args(args)
    if args.mode in {"i2va", "fl2va"} and args.reference_image:
        raise UserError(
            "--reference-image can only be used with --mode ref2va"
        )
    if args.mode == "i2va" and args.last_image:
        raise UserError("--last-image requires --mode fl2va")
    if args.mode == "ref2va" and (args.first_image or args.last_image):
        raise UserError(
            "--first-image and --last-image cannot be used with --mode ref2va"
        )
    if args.mode == "i2va":
        if not args.first_image:
            raise UserError("I2VA requires --first-image")
        images = [args.first_image]
        default_workflow = DEFAULT_IMAGE_WORKFLOW
    elif args.mode == "fl2va":
        if not args.first_image or not args.last_image:
            raise UserError("FL2VA requires --first-image and --last-image")
        images = [args.first_image, args.last_image]
        default_workflow = DEFAULT_IMAGE_WORKFLOW
    else:
        images = args.reference_image or []
        if not 1 <= len(images) <= 9:
            raise UserError("Ref2VA requires 1-9 --reference-image values")
        default_workflow = DEFAULT_REFERENCE_WORKFLOW
    image_paths = [Path(path).expanduser().resolve() for path in images]
    for image_path in image_paths:
        if not image_path.is_file():
            raise UserError(f"Image not found: {image_path}")
    workflow = (
        Path(args.workflow).expanduser().resolve()
        if args.workflow
        else default_workflow.resolve()
    )
    if not workflow.is_file():
        raise UserError(f"Workflow not found: {workflow}")
    if not math.isfinite(args.duration) or args.duration <= 0:
        raise UserError("--duration must be a finite positive number")
    if not math.isfinite(args.megapixels) or args.megapixels <= 0:
        raise UserError("--megapixels must be a finite positive number")
    if args.steps is not None and args.steps <= 0:
        raise UserError("--steps must be positive")
    if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
        raise UserError("--poll-interval must be a finite positive number")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        raise UserError("--timeout must be a finite non-negative number")
    output_dir = Path(args.output_dir or workspace).expanduser().resolve()
    output_prefix = args.output_prefix or f"video/H3_{args.mode}_{job_id}"
    return {
        "job_id": job_id,
        "api_url": api_url,
        "workspace": str(workspace),
        "mode": args.mode,
        "workflow": str(workflow),
        "images": [str(path) for path in image_paths],
        "prompt": prompt,
        "duration": args.duration,
        "megapixels": args.megapixels,
        "aspect_ratio": normalize_aspect_ratio(args.aspect_ratio),
        "steps": args.steps,
        "ref_image_size": args.ref_image_size,
        "output_prefix": output_prefix,
        "output_dir": str(output_dir),
        "seed": args.seed,
        "poll_interval": args.poll_interval,
        "timeout": args.timeout,
        "created_at": utc_now(),
    }


def create_job(args: argparse.Namespace, api_url: str) -> tuple[Path, dict]:
    workspace = Path(args.workspace or os.getcwd()).expanduser().resolve()
    jobs_root = (
        Path(args.jobs_dir).expanduser().resolve()
        if args.jobs_dir
        else workspace / ".comfyui-jobs"
    )
    job_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    job_dir = jobs_root / job_id
    spec = build_spec(args, job_id, workspace, api_url)
    job_dir.mkdir(parents=True, exist_ok=False)
    spec_path = job_dir / "spec.json"
    write_json_atomic(spec_path, spec)
    status = {
        "job_id": job_id,
        "state": "queued",
        "mode": spec["mode"],
        "job_dir": str(job_dir.resolve()),
        "spec_path": str(spec_path.resolve()),
        "created_at": spec["created_at"],
        "updated_at": utc_now(),
    }
    write_json_atomic(job_dir / "status.json", status)
    return job_dir, status


def command_generate(args: argparse.Namespace) -> int:
    job_dir, status = create_job(args, get_api_url())
    spec_path = job_dir / "spec.json"
    if args.foreground:
        result = execute_spec(spec_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("state") == "completed" else 1
    log_path = job_dir / "worker.log"
    try:
        with log_path.open("ab", buffering=0) as log_handle:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "_worker",
                    "--spec",
                    str(spec_path),
                ],
                cwd=status["job_dir"],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        update_status(job_dir / "status.json", "failed", error=str(exc))
        raise UserError(f"Cannot start background worker: {exc}") from exc
    status = update_status(
        job_dir / "status.json",
        "background",
        worker_pid=process.pid,
        log_path=str(log_path.resolve()),
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def resolve_job(args: argparse.Namespace) -> Path:
    workspace = Path(args.workspace or os.getcwd()).expanduser().resolve()
    jobs_root = (
        Path(args.jobs_dir).expanduser().resolve()
        if args.jobs_dir
        else workspace / ".comfyui-jobs"
    )
    if args.job:
        candidate = Path(args.job).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        candidate = jobs_root / args.job
        if candidate.is_dir():
            return candidate.resolve()
        raise UserError(f"Job not found: {args.job}")
    jobs = sorted((path for path in jobs_root.glob("*") if path.is_dir()), reverse=True)
    if not jobs:
        raise UserError(f"No jobs found in {jobs_root}")
    return jobs[0].resolve()


def command_status(args: argparse.Namespace) -> int:
    job_dir = resolve_job(args)
    status = read_json(job_dir / "status.json")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 1 if status.get("state") in {"failed", "timed_out"} else 0


def command_wait(args: argparse.Namespace) -> int:
    if not math.isfinite(args.interval) or args.interval <= 0:
        raise UserError("--interval must be a finite positive number")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        raise UserError("--timeout must be a finite non-negative number")
    job_dir = resolve_job(args)
    status_path = job_dir / "status.json"
    started = time.monotonic()
    last_state = None
    while True:
        status = read_json(status_path)
        state = status.get("state")
        if state != last_state:
            print(f"{utc_now()} state={state}", file=sys.stderr, flush=True)
            last_state = state
        if state in TERMINAL_STATES:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if state == "completed" else 1
        if args.timeout and time.monotonic() - started > args.timeout:
            raise UserError("Local wait timed out; the background worker continues running")
        time.sleep(args.interval)


def command_check(args: argparse.Namespace) -> int:
    api_url = get_api_url()
    image_workflow_path = (
        Path(args.image_workflow).expanduser().resolve()
        if args.image_workflow
        else DEFAULT_IMAGE_WORKFLOW.resolve()
    )
    reference_workflow_path = (
        Path(args.reference_workflow).expanduser().resolve()
        if args.reference_workflow
        else DEFAULT_REFERENCE_WORKFLOW.resolve()
    )
    image_workflow = read_json(image_workflow_path)
    reference_workflow = read_json(reference_workflow_path)
    one_node(image_workflow, "MiniMaxH3ImageToVideo")
    one_node(reference_workflow, "MiniMaxH3ReferenceToVideo")
    api = ComfyApi(api_url)
    stats = api.json("GET", "/system_stats", timeout=30)
    image_info = api.json("GET", "/object_info/MiniMaxH3ImageToVideo", timeout=30)
    ref_info = api.json("GET", "/object_info/MiniMaxH3ReferenceToVideo", timeout=30)
    image_node_info = image_info.get("MiniMaxH3ImageToVideo")
    if not isinstance(image_node_info, dict):
        raise UserError("Remote ComfyUI does not provide MiniMaxH3ImageToVideo")
    ref_node_info = ref_info.get("MiniMaxH3ReferenceToVideo")
    if not isinstance(ref_node_info, dict):
        raise UserError("Remote ComfyUI does not provide MiniMaxH3ReferenceToVideo")
    image_optional = image_node_info.get("input", {}).get("optional", {})
    ref_images = ref_node_info.get("input", {}).get("optional", {}).get("ref_images")
    if (
        not isinstance(ref_images, list)
        or len(ref_images) < 2
        or not isinstance(ref_images[1], dict)
        or not isinstance(ref_images[1].get("template"), dict)
    ):
        raise UserError("Remote MiniMaxH3ReferenceToVideo has no ref_images template")
    ref_template = ref_images[1]["template"]
    result = {
        "ok": (
            "first_frame" in image_optional
            and "last_frame" in image_optional
            and ref_template.get("max", 0) >= 9
        ),
        "api_url": api_url,
        "comfyui_version": stats.get("system", {}).get("comfyui_version"),
        "device": (stats.get("devices") or [{}])[0].get("name"),
        "i2va_first_frame": "first_frame" in image_optional,
        "fl2va_last_frame": "last_frame" in image_optional,
        "ref2va_max_images": ref_template.get("max"),
        "workflows": {
            "i2va_fl2va": str(image_workflow_path),
            "ref2va": str(reference_workflow_path),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def add_workspace_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", help="Workspace root; defaults to current directory")
    parser.add_argument("--jobs-dir", help="Job state directory; defaults to .comfyui-jobs")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate MiniMax H3 I2VA, FL2VA, or Ref2VA videos through ComfyUI",
        epilog=f"Server configuration: set {API_URL_ENV} in the environment.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Validate server and workflow compatibility")
    check.add_argument("--image-workflow")
    check.add_argument("--reference-workflow")
    check.set_defaults(handler=command_check)

    generate = subparsers.add_parser("generate", help="Submit and monitor a generation job")
    add_workspace_args(generate)
    generate.add_argument("--mode", choices=["i2va", "fl2va", "ref2va"], required=True)
    prompt_group = generate.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    generate.add_argument("--first-image")
    generate.add_argument("--last-image")
    generate.add_argument("--reference-image", action="append")
    generate.add_argument("--workflow")
    generate.add_argument("--duration", type=float, default=15.0)
    generate.add_argument("--megapixels", type=float, default=0.4)
    generate.add_argument("--aspect-ratio")
    generate.add_argument(
        "--steps", type=int, help="Override the workflow BasicScheduler step count"
    )
    generate.add_argument("--ref-image-size", choices=["match", "max"], default="match")
    generate.add_argument("--output-prefix")
    generate.add_argument("--output-dir")
    generate.add_argument("--seed", type=int)
    generate.add_argument("--poll-interval", type=float, default=20.0)
    generate.add_argument(
        "--timeout", type=float, default=0, help="Monitor timeout in seconds; 0 disables"
    )
    generate.add_argument("--foreground", action="store_true")
    generate.set_defaults(handler=command_generate)

    for name, handler in (("status", command_status), ("wait", command_wait)):
        command = subparsers.add_parser(name)
        add_workspace_args(command)
        command.add_argument("--job", help="Job id/path; defaults to latest job")
        if name == "wait":
            command.add_argument("--interval", type=float, default=2.0)
            command.add_argument("--timeout", type=float, default=0)
        command.set_defaults(handler=handler)

    worker = subparsers.add_parser("_worker")
    worker.add_argument("--spec", required=True)
    worker.set_defaults(
        handler=lambda args: (
            print(json.dumps(execute_spec(Path(args.spec)), ensure_ascii=False, indent=2))
            or 0
        )
    )
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except UserError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
