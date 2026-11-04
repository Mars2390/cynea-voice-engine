"""Call recording storage: Cloudflare R2 in production, local disk otherwise.

The personas announce on the opening turn that the call is recorded, so the
audio has to actually exist somewhere. Which backend runs is decided by the
environment, never by a flag in code:

    R2 (S3-compatible, free tier 10 GB, no egress fees) when all of
        R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
    are set. Playback goes through a presigned GET that expires.

    Local disk otherwise, under RECORDINGS_DIR (default: recordings/ in the
    repo). Playback goes through /calls/{id}/recording/file with an
    HMAC-signed, expiring query string, because an <audio> element cannot
    send the bearer token the rest of the API requires.

On Vercel the filesystem is read-only apart from /tmp, and /tmp does not
outlive the instance — so without R2 a deployed recording is effectively
temporary. The module says so in the log rather than pretending otherwise.

Every object is addressed by an `audio_url` stored on the call:
    r2://<bucket>/<key>      or      local://<key>
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import pathlib
import time
import uuid
from typing import Optional, Tuple

log = logging.getLogger("cynea.recordings")

URL_TTL_S = 600                      # how long a playback link stays valid
MAX_BYTES = int(os.getenv("RECORDING_MAX_BYTES", str(25 * 1024 * 1024)))

# What the upload endpoint accepts, and the extension each is stored under.
CONTENT_TYPES = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/webm": "webm", "audio/ogg": "ogg",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
}

_warned_tmp = False


class StorageError(RuntimeError):
    """The backend refused or could not be reached."""


def retention_days() -> int:
    return int(os.getenv("RECORDING_RETENTION_DAYS", "30"))


# ── backend selection ─────────────────────────────────────────────────────
def _r2_settings() -> Optional[dict]:
    env = {k: os.getenv(k, "").strip() for k in
           ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")}
    if env["R2_ACCOUNT_ID"] and env["R2_ACCESS_KEY_ID"]:
        missing = [k for k, v in env.items() if not v]
        if missing:
            raise StorageError(f"R2 is partly configured; also set {', '.join(missing)}")
        return env
    return None


def backend() -> str:
    return "r2" if _r2_settings() else "local"


def _r2_client(cfg: dict):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _local_root() -> pathlib.Path:
    global _warned_tmp
    explicit = os.getenv("RECORDINGS_DIR", "").strip()
    if explicit:
        root = pathlib.Path(explicit)
    elif os.getenv("VERCEL"):
        root = pathlib.Path("/tmp/cynea-recordings")
        if not _warned_tmp:
            log.warning("No R2 configured on Vercel: recordings go to /tmp and are lost "
                        "when the instance is recycled. Set the R2_* variables.")
            _warned_tmp = True
    else:
        root = pathlib.Path(__file__).resolve().parent.parent / "recordings"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _local_path(key: str) -> pathlib.Path:
    root = _local_root().resolve()
    path = (root / key).resolve()
    if root not in path.parents:                 # no ../ escapes
        raise StorageError("invalid storage key")
    return path


# ── operations ────────────────────────────────────────────────────────────
def store(call_id: str, data: bytes, content_type: str) -> Tuple[str, str]:
    """Save the audio. Returns (storage_key, audio_url)."""
    ext = CONTENT_TYPES[content_type]
    key = f"calls/{call_id}/{uuid.uuid4().hex}.{ext}"
    cfg = _r2_settings()
    if cfg:
        try:
            _r2_client(cfg).put_object(Bucket=cfg["R2_BUCKET"], Key=key, Body=data,
                                       ContentType=content_type)
        except Exception as exc:
            raise StorageError(f"R2 upload failed: {exc}") from exc
        return key, f"r2://{cfg['R2_BUCKET']}/{key}"
    path = _local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key, f"local://{key}"


def delete(audio_url: str) -> None:
    """Remove the object behind an audio_url. Missing objects are fine."""
    if audio_url.startswith("r2://"):
        bucket, _, key = audio_url[5:].partition("/")
        cfg = _r2_settings()
        if not cfg:
            raise StorageError("an R2 recording exists but R2 is not configured")
        try:
            _r2_client(cfg).delete_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"R2 delete failed: {exc}") from exc
    elif audio_url.startswith("local://"):
        _local_path(audio_url[8:]).unlink(missing_ok=True)


def playback_url(audio_url: str, call_id: str, api_base: str) -> str:
    """A URL an <audio> element can load directly, valid for URL_TTL_S."""
    if audio_url.startswith("r2://"):
        bucket, _, key = audio_url[5:].partition("/")
        cfg = _r2_settings()
        if not cfg:
            raise StorageError("an R2 recording exists but R2 is not configured")
        return _r2_client(cfg).generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=URL_TTL_S)
    exp = int(time.time()) + URL_TTL_S
    return f"{api_base.rstrip('/')}/calls/{call_id}/recording/file?exp={exp}&sig={_sign(call_id, exp)}"


def local_file(audio_url: str) -> pathlib.Path:
    if not audio_url.startswith("local://"):
        raise StorageError("not a local recording")
    return _local_path(audio_url[8:])


# ── signed local links ────────────────────────────────────────────────────
def _secret() -> bytes:
    from cynea import auth
    return auth._session_secret()


def _sign(call_id: str, exp: int) -> str:
    return hmac.new(_secret(), f"rec:{call_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def check_signature(call_id: str, exp: int, sig: str) -> bool:
    if exp < time.time():
        return False
    return hmac.compare_digest(_sign(call_id, exp), sig or "")
