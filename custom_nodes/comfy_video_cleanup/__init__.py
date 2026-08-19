"""ComfyUI server extension that lets comfy-video delete the files it created.

ComfyUI's own HTTP API has no route for removing files from the input, output,
or temp directories, so this extension adds one:

    DELETE /comfy_video/file?type=output&subfolder=video&filename=name.mp4

The query parameters are the same ones ComfyUI's ``/view`` route takes, so a
client can delete exactly what it just downloaded.

Install by copying this directory into ``ComfyUI/custom_nodes/`` and restarting
ComfyUI. The route is also reachable under the ``/api`` prefix that ComfyUI
mirrors onto every non-static route.
"""

import logging
import os

from aiohttp import web

import folder_paths
from server import PromptServer

# Only files whose basename starts with this prefix may be deleted. comfy-video
# names every upload and every rendered video with it, so the endpoint cannot
# touch anything else on a shared server. Set to None to allow any file inside
# the directories listed in ALLOWED_TYPES.
REQUIRED_PREFIX = "comfy-video-"
ALLOWED_TYPES = ("input", "output", "temp")


def _json_error(status, message):
    return web.json_response({"deleted": False, "error": message}, status=status)


def _resolve(dir_type, subfolder, filename):
    """Resolve request parameters to an absolute path.

    Returns ``(path, None)`` on success or ``(None, response)`` on rejection.
    """
    if dir_type not in ALLOWED_TYPES:
        return None, _json_error(400, f"type must be one of {', '.join(ALLOWED_TYPES)}")
    if not filename:
        return None, _json_error(400, "filename is required")
    if REQUIRED_PREFIX and not os.path.basename(filename).startswith(REQUIRED_PREFIX):
        return None, _json_error(403, f"filename must start with {REQUIRED_PREFIX!r}")

    base_dir = folder_paths.get_directory_by_type(dir_type)
    if not base_dir:
        return None, _json_error(400, f"ComfyUI has no {dir_type} directory")

    # realpath both operands so a symlink inside the directory cannot escape it.
    base_dir = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base_dir, subfolder, filename))
    try:
        contained = os.path.commonpath((base_dir, path)) == base_dir
    except ValueError:  # different drives on Windows
        contained = False
    if not contained:
        return None, _json_error(403, "path escapes the target directory")
    return path, None


@PromptServer.instance.routes.delete("/comfy_video/file")
async def delete_comfy_video_file(request):
    query = request.rel_url.query
    path, error = _resolve(
        query.get("type", "output"),
        query.get("subfolder", ""),
        query.get("filename", ""),
    )
    if error is not None:
        return error

    if not os.path.isfile(path):
        return web.json_response({"deleted": False, "reason": "not_found"})
    try:
        os.remove(path)
    except OSError as exc:
        logging.warning("comfy_video_cleanup: cannot delete %s: %s", path, exc)
        return _json_error(500, str(exc))

    logging.info("comfy_video_cleanup: deleted %s", path)
    return web.json_response({"deleted": True})


# This extension only registers a route; it contributes no graph nodes.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
