"""upkrade: a minimal Expo Updates server (protocol v1) for arkitektio/pokket.

Everything lives in one RustFS bucket:

- ``assets/<md5>``: file bytes, content-addressed and written once.
- ``updates/<id>.json``: one published update (manifest without URLs).
- ``pointers/<channel>/<platform>/<runtime>.json``: what that runtime gets
  right now (an update or "roll back to embedded"), plus its history.

A publish writes assets, then the update, then the pointer, so a manifest
never references an asset that is not there yet. The server is stateless.

Spec: https://docs.expo.dev/technical-specs/expo-updates-1/
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("upkrade")
logging.basicConfig(level=os.environ.get("UPKRADE_LOG_LEVEL", "INFO").upper())

CONFIG = yaml.safe_load(Path(os.environ.get("UPKRADE_CONFIG", "/workspace/config.yaml")).read_text())
PUBLIC_URL = CONFIG["public_url"].rstrip("/")
DEFAULT_CHANNEL = CONFIG.get("default_channel", "production")
BUCKET = CONFIG["s3"]["bucket"]
KEYID = CONFIG.get("signing_keyid", "main")

PLATFORMS = {"ios", "android"}
# Channels and runtime versions become object keys: keep them to a safe alphabet.
NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ASSET_KEY = re.compile(r"^[0-9a-f]{32}$")
UPDATE_ID = re.compile(r"^[0-9a-f-]{36}$")

s3 = boto3.client(
    "s3",
    endpoint_url=CONFIG["s3"]["endpoint_url"],
    aws_access_key_id=CONFIG["s3"]["access_key"],
    aws_secret_access_key=CONFIG["s3"]["secret_key"],
    region_name=CONFIG["s3"].get("region", "us-east-1"),
    config=BotoConfig(s3={"addressing_style": "path"}, signature_version="s3v4"),
)

_key_path = Path(CONFIG.get("signing_key", "/keys/private-key.pem"))
PRIVATE_KEY = (
    serialization.load_pem_private_key(_key_path.read_bytes(), password=None) if _key_path.exists() else None
)
if PRIVATE_KEY is None:
    log.warning("No signing key at %s: clients that expect a signature will be refused", _key_path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- storage


def ensure_bucket() -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
    except ClientError:
        s3.create_bucket(Bucket=BUCKET)
        log.info("Created bucket %s", BUCKET)


def get_json(key: str) -> dict | None:
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def put_json(key: str, value: dict) -> None:
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(value).encode(), ContentType="application/json")


def pointer_key(channel: str, platform: str, runtime: str) -> str:
    return f"pointers/{channel}/{platform}/{runtime}.json"


_updates: dict[str, dict] = {}


def get_update(update_id: str) -> dict | None:
    # Updates are immutable once written, so hits are cached for good; misses never are.
    if update_id not in _updates:
        update = get_json(f"updates/{update_id}.json")
        if update is None:
            return None
        _updates[update_id] = update
    return _updates[update_id]


def set_pointer(channel: str, platform: str, runtime: str, current: dict) -> dict:
    key = pointer_key(channel, platform, runtime)
    pointer = get_json(key) or {"history": []}
    pointer["current"] = current
    pointer["history"] = ([{**current, "at": now_iso()}] + pointer.get("history", []))[:100]
    put_json(key, pointer)
    return pointer


# --------------------------------------------------------------------------- protocol


def sign(body: bytes) -> str:
    sig = PRIVATE_KEY.sign(body, padding.PKCS1v15(), hashes.SHA256())
    # A structured-field dictionary (RFC 8941), as expo-updates parses it.
    return f'sig="{base64.b64encode(sig).decode()}", keyid="{KEYID}"'


def multipart(parts: list[tuple[str, dict]], signed: bool) -> Response:
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in parts:
        body = json.dumps(value).encode()
        headers = [
            f'Content-Disposition: form-data; name="{name}"',
            "Content-Type: application/json; charset=utf-8",
        ]
        if signed and name in ("manifest", "directive"):
            headers.append(f"expo-signature: {sign(body)}")
        chunks.append(f"--{boundary}\r\n".encode() + "\r\n".join(headers).encode() + b"\r\n\r\n" + body + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return Response(
        b"".join(chunks),
        media_type=f"multipart/mixed; boundary={boundary}",
        headers={
            "expo-protocol-version": "1",
            "expo-sfv-version": "0",
            "cache-control": "private, max-age=0",
        },
    )


def with_urls(asset: dict) -> dict:
    return {**asset, "url": f"{PUBLIC_URL}/assets/{asset['key']}"}


def build_manifest(update: dict) -> dict:
    return {
        "id": update["id"],
        "createdAt": update["createdAt"],
        "runtimeVersion": update["runtimeVersion"],
        "launchAsset": with_urls(update["launchAsset"]),
        "assets": [with_urls(a) for a in update["assets"]],
        "metadata": {},
        "extra": {"expoClient": update.get("expoClient") or {}},
    }


async def manifest(request: Request) -> Response:
    h, q = request.headers, request.query_params
    platform = h.get("expo-platform") or q.get("platform")
    runtime = h.get("expo-runtime-version") or q.get("runtime-version")
    channel = h.get("expo-channel-name")
    protocol = int(h.get("expo-protocol-version", "0") or 0)
    current_id = h.get("expo-current-update-id")
    expects_signature = "expo-expect-signature" in h

    if not channel:
        # Builds made by `eas build` should send the profile's channel; log when one doesn't.
        log.warning("No expo-channel-name from %s/%s, using %s", platform, runtime, DEFAULT_CHANNEL)
        channel = DEFAULT_CHANNEL
    if platform not in PLATFORMS or not runtime or not NAME.match(runtime) or not NAME.match(channel):
        return PlainTextResponse("expo-platform, expo-runtime-version and expo-channel-name are required", 400)
    if expects_signature and PRIVATE_KEY is None:
        return PlainTextResponse("This server has no signing key", 500)

    log.info("check channel=%s platform=%s runtime=%s current=%s", channel, platform, runtime, current_id)
    pointer = await run_in_threadpool(get_json, pointer_key(channel, platform, runtime))
    current = (pointer or {}).get("current")

    if current is None or current.get("type") == "rollBackToEmbedded":
        if protocol < 1:
            return PlainTextResponse("No update for this runtime", 404)
        if current is None:
            return multipart([("directive", {"type": "noUpdateAvailable"})], expects_signature)
        directive = {"type": "rollBackToEmbedded", "parameters": {"commitTime": current["commitTime"]}}
        return multipart([("directive", directive)], expects_signature)

    update = await run_in_threadpool(get_update, current["id"])
    if update is None:
        log.error("Pointer %s names missing update %s", pointer_key(channel, platform, runtime), current["id"])
        return PlainTextResponse("Update missing", 500)
    if protocol >= 1 and current_id and current_id.lower() == update["id"]:
        return multipart([("directive", {"type": "noUpdateAvailable"})], expects_signature)

    body = build_manifest(update)
    extensions = {"assetRequestHeaders": {a["key"]: {} for a in [update["launchAsset"], *update["assets"]]}}
    return multipart([("manifest", body), ("extensions", extensions)], expects_signature)


async def asset(request: Request) -> Response:
    key = request.path_params["key"]
    if not ASSET_KEY.match(key):
        return PlainTextResponse("Not found", 404)
    try:
        obj = await run_in_threadpool(s3.get_object, Bucket=BUCKET, Key=f"assets/{key}")
    except ClientError:
        return PlainTextResponse("Not found", 404)
    return StreamingResponse(
        obj["Body"].iter_chunks(64 * 1024),
        media_type=obj.get("ContentType", "application/octet-stream"),
        headers={
            "Content-Length": str(obj["ContentLength"]),
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


# --------------------------------------------------------------------------- publishing


def authorized(request: Request) -> bool:
    given = request.headers.get("authorization", "")
    return hmac.compare_digest(given.encode(), f"Bearer {CONFIG['publish_token']}".encode())


# The slim image has no /etc/mime.types; pin what React Native bundles ship.
MIME = {
    "ttf": "font/ttf",
    "otf": "font/otf",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "json": "application/json",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "mp4": "video/mp4",
}


def upload_file(path: Path, ext: str) -> dict:
    data = path.read_bytes()
    key = hashlib.md5(data).hexdigest()
    content_type = MIME.get(ext.lower()) or mimetypes.guess_type(f"x.{ext}")[0] or "application/octet-stream"
    try:
        s3.head_object(Bucket=BUCKET, Key=f"assets/{key}")
    except ClientError:
        s3.put_object(Bucket=BUCKET, Key=f"assets/{key}", Body=data, ContentType=content_type)
    return {
        "hash": base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(),
        "key": key,
        "contentType": content_type,
        "fileExtension": f".{ext}",
    }


def publish_dist(dist: Path, channel: str, platform: str, runtime: str, message: str) -> dict:
    metadata = json.loads((dist / "metadata.json").read_text())
    files = metadata["fileMetadata"][platform]
    expo_client = None
    if (dist / "expoConfig.json").exists():
        expo_client = json.loads((dist / "expoConfig.json").read_text())

    def inside(rel: str) -> Path:
        path = (dist / rel).resolve()
        if not path.is_relative_to(dist.resolve()):
            raise ValueError(f"{rel} escapes the archive")
        return path

    launch = upload_file(inside(files["bundle"]), "bundle")
    launch["contentType"] = "application/javascript"
    assets = [upload_file(inside(a["path"]), a["ext"]) for a in files["assets"]]

    update = {
        "id": str(uuid.uuid4()),
        "createdAt": now_iso(),
        "runtimeVersion": runtime,
        "channel": channel,
        "platform": platform,
        "message": message,
        "launchAsset": launch,
        "assets": assets,
        "expoClient": expo_client,
    }
    put_json(f"updates/{update['id']}.json", update)
    set_pointer(channel, platform, runtime, {"type": "update", "id": update["id"]})
    log.info("published %s to %s/%s/%s (%d assets): %s", update["id"], channel, platform, runtime, len(assets), message)
    return update


async def publish(request: Request) -> Response:
    if not authorized(request):
        return PlainTextResponse("Unauthorized", 401)
    form = await request.form(max_part_size=1024 * 1024)
    channel, platform = form.get("channel", DEFAULT_CHANNEL), form.get("platform")
    runtime, message = form.get("runtimeVersion"), form.get("message", "")
    archive = form.get("archive")
    if platform not in PLATFORMS or not runtime or not NAME.match(runtime) or not NAME.match(channel):
        return PlainTextResponse("platform, runtimeVersion and channel are required", 400)
    if archive is None or isinstance(archive, str):
        return PlainTextResponse("archive (a tar.gz of the expo export) is required", 400)

    def run() -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=archive.file, mode="r:gz") as tar:
                tar.extractall(tmp, filter="data")
            return publish_dist(Path(tmp), channel, platform, runtime, message)

    try:
        update = await run_in_threadpool(run)
    except (KeyError, ValueError, tarfile.TarError, json.JSONDecodeError) as e:
        return PlainTextResponse(f"Not an expo export for {platform}: {e!r}", 400)
    return JSONResponse({"id": update["id"], "createdAt": update["createdAt"], "assets": len(update["assets"])})


async def rollback(request: Request) -> Response:
    """Repoint a runtime: `to=<update id>` (one published to it before) or `embedded=true`."""
    if not authorized(request):
        return PlainTextResponse("Unauthorized", 401)
    form = await request.form()
    channel, platform, runtime = form.get("channel", DEFAULT_CHANNEL), form.get("platform"), form.get("runtimeVersion")
    if platform not in PLATFORMS or not runtime or not NAME.match(runtime) or not NAME.match(channel):
        return PlainTextResponse("platform, runtimeVersion and channel are required", 400)

    if form.get("embedded") in ("1", "true", "yes"):
        current = {"type": "rollBackToEmbedded", "commitTime": now_iso()}
    else:
        target = form.get("to", "")
        update = await run_in_threadpool(get_update, target) if UPDATE_ID.match(target) else None
        if update is None or (update["channel"], update["platform"], update["runtimeVersion"]) != (channel, platform, runtime):
            return PlainTextResponse("`to` must be an update published to this channel/platform/runtime", 400)
        current = {"type": "update", "id": target}
    pointer = await run_in_threadpool(set_pointer, channel, platform, runtime, current)
    log.info("rollback %s/%s/%s -> %s", channel, platform, runtime, current)
    return JSONResponse(pointer)


async def status(request: Request) -> Response:
    """What a runtime currently gets, with its history (authorized: it lists update ids)."""
    if not authorized(request):
        return PlainTextResponse("Unauthorized", 401)
    q = request.query_params
    channel, platform, runtime = q.get("channel", DEFAULT_CHANNEL), q.get("platform"), q.get("runtimeVersion")
    if platform not in PLATFORMS or not runtime or not NAME.match(runtime) or not NAME.match(channel):
        return PlainTextResponse("platform, runtimeVersion and channel are required", 400)
    return JSONResponse(await run_in_threadpool(get_json, pointer_key(channel, platform, runtime)) or {})


@contextlib.asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(ensure_bucket)
    yield


async def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/api/manifest", manifest, methods=["GET"]),
        Route("/assets/{key}", asset, methods=["GET"]),
        Route("/api/publish", publish, methods=["POST"]),
        Route("/api/rollback", rollback, methods=["POST"]),
        Route("/api/status", status, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ],
    lifespan=lifespan,
)
