"""Minimal async R2 object client over the S3-compatible data plane.

Speaks SigV4 against <account>.r2.cloudflarestorage.com -- still Cloudflare,
same bucket, same bill. The credentials are DERIVED from the existing
R2-scoped API token (access key id = token id, secret = sha256 of the token
value; Cloudflare documents this equivalence), so no separate secret exists
to rotate. We moved off the api.cloudflare.com REST object endpoint because
that is the management control plane, capped at ~1200 requests/5min globally
per token -- a full sync plus a backfill would silently thin corpus coverage
against it. The data plane has no such request cap.

SigV4 is hand-rolled (~40 lines) rather than importing boto3: the botocore
stack is sync, heavy, and we need exactly three verbs against one endpoint.
The signed-header set is fixed (host, x-amz-content-sha256, x-amz-date);
Content-Type rides unsigned, which S3 semantics allow. Payload hashes are
signed for real: callers that already know sha256(body) -- the corpus always
does for originals, it IS the key -- pass it; bytes payloads compute it.
"""

import asyncio
import hashlib
import hmac
from datetime import datetime, timezone
from typing import IO, Optional, Union
from urllib.parse import quote

import aiohttp

from config import get_logger

logger = get_logger(__name__).bind(component="corpus_r2")

_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SIGNED_HEADERS = "host;x-amz-content-sha256;x-amz-date"
_REGION = "auto"
_SERVICE = "s3"


class R2Error(Exception):
    """R2 request failed after retries."""


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class R2Client:
    """Async SigV4 client bound to one bucket. Lazily owns an aiohttp session."""

    def __init__(self, account_id: str, access_key_id: str, secret_access_key: str, bucket: str):
        self._host = f"{account_id}.r2.cloudflarestorage.com"
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self.bucket = bucket
        self._session: Optional[aiohttp.ClientSession] = None

    def _path(self, key: str) -> str:
        # Canonical URI: segment-encoded, slashes preserved. Corpus keys are
        # hex + prefix + extension, but encode defensively anyway.
        return quote(f"/{self.bucket}/{key}", safe="/-._~")

    def _signed_headers(self, method: str, path: str, payload_sha256: str) -> dict:
        """Produce the SigV4 headers for one attempt. Regenerated per retry
        so a backoff can never push x-amz-date past the clock-skew window."""
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        canonical_request = "\n".join([
            method,
            path,
            "",  # canonical query string -- corpus ops never use one
            f"host:{self._host}\nx-amz-content-sha256:{payload_sha256}\nx-amz-date:{amz_date}\n",
            _SIGNED_HEADERS,
            payload_sha256,
        ])
        scope = f"{datestamp}/{_REGION}/{_SERVICE}/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        key = _hmac(("AWS4" + self._secret_access_key).encode("utf-8"), datestamp)
        key = _hmac(key, _REGION)
        key = _hmac(key, _SERVICE)
        key = _hmac(key, "aws4_request")
        signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        return {
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_sha256,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self._access_key_id}/{scope}, "
                f"SignedHeaders={_SIGNED_HEADERS}, Signature={signature}"
            ),
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300, connect=30)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def put(
        self,
        key: str,
        data: Union[bytes, IO],
        content_type: str = "application/octet-stream",
        payload_sha256: Optional[str] = None,
        content_length: Optional[int] = None,
    ) -> None:
        """Upload an object. `data` may be bytes or a seekable binary file
        object (rewound before each retry). File objects MUST come with
        payload_sha256 and content_length: the hash is signed, and an explicit
        length keeps aiohttp from chunked transfer-encoding, which S3-style
        endpoints reject on plain PUTs."""
        if isinstance(data, bytes):
            if payload_sha256 is None:
                payload_sha256 = hashlib.sha256(data).hexdigest()
            content_length = len(data)
        elif payload_sha256 is None or content_length is None:
            raise ValueError("file-object put requires payload_sha256 and content_length")

        path = self._path(key)
        last_error: Optional[str] = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if not isinstance(data, bytes):
                data.seek(0)
            headers = self._signed_headers("PUT", path, payload_sha256)
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(content_length)
            try:
                session = await self._get_session()
                async with session.put(
                    f"https://{self._host}{path}", data=data, headers=headers
                ) as resp:
                    if resp.status < 300:
                        return
                    body = (await resp.text())[:300]
                    last_error = f"HTTP {resp.status}: {body}"
                    if resp.status < 500:
                        break  # auth/signature/validation errors do not heal on retry
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                last_error = f"{type(e).__name__}: {e}"
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BASE_DELAY * 2 ** (attempt - 1))
        raise R2Error(f"put {self.bucket}/{key} failed: {last_error}")

    async def get(self, key: str) -> Optional[bytes]:
        """Fetch an object's bytes. Returns None on 404."""
        path = self._path(key)
        last_error: Optional[str] = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            headers = self._signed_headers("GET", path, _EMPTY_SHA256)
            try:
                session = await self._get_session()
                async with session.get(f"https://{self._host}{path}", headers=headers) as resp:
                    if resp.status == 404:
                        return None
                    if resp.status < 300:
                        return await resp.read()
                    body = (await resp.text())[:300]
                    last_error = f"HTTP {resp.status}: {body}"
                    if resp.status < 500:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                last_error = f"{type(e).__name__}: {e}"
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BASE_DELAY * 2 ** (attempt - 1))
        raise R2Error(f"get {self.bucket}/{key} failed: {last_error}")

    async def delete(self, key: str) -> None:
        """Delete an object. Missing objects are not an error."""
        path = self._path(key)
        headers = self._signed_headers("DELETE", path, _EMPTY_SHA256)
        session = await self._get_session()
        async with session.delete(f"https://{self._host}{path}", headers=headers) as resp:
            if resp.status >= 300 and resp.status != 404:
                body = (await resp.text())[:300]
                raise R2Error(f"delete {self.bucket}/{key} failed: HTTP {resp.status}: {body}")
