"""Storage abstraction: local filesystem (dev) and S3-compatible (prod).

Files are streamed in both directions; nothing here loads a whole upload or download
into memory:

* uploads are validated from the first few KB (magic bytes vs. extension), then copied
  in chunks while counting bytes, so an oversize file is rejected mid-stream;
* local downloads use ``FileResponse`` (sendfile), S3 downloads stream the object body
  in chunks via a worker thread.
"""
import asyncio
import os
import shutil
import tempfile
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote

from starlette.responses import FileResponse, Response, StreamingResponse

from app.core.config import get_settings

CHUNK_SIZE = 1024 * 1024
SNIFF_BYTES = 8192

ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}

# Leading bytes each extension must start with. Office formats are ZIP containers.
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")
MAGIC: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".zip": _ZIP,
    ".docx": _ZIP,
    ".xlsx": _ZIP,
    ".pptx": _ZIP,
}


def max_upload_bytes() -> int:
    return get_settings().max_upload_mb * 1024 * 1024


class StorageError(Exception):
    pass


class UploadTooLarge(StorageError):
    pass


def sniff_matches(ext: str, head: bytes) -> bool:
    if ext == ".csv":
        if b"\x00" in head:
            return False
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A multi-byte character may be cut at the sniff boundary.
            if exc.start < len(head) - 4:
                return False
        return True
    return any(head.startswith(sig) for sig in MAGIC.get(ext, ()))


def validate_upload_head(filename: str, head: bytes, size: int | None) -> tuple[str, str]:
    """Validate type and size from the first bytes of an upload. Returns (ext, content_type)."""
    limit = max_upload_bytes()
    if size is not None and size > limit:
        raise UploadTooLarge(f"File exceeds maximum size of {get_settings().max_upload_mb} MB")
    if not head:
        raise StorageError("Empty file")
    ext = os.path.splitext(filename)[1].lower()
    content_type = ALLOWED_EXTENSIONS.get(ext)
    if content_type is None:
        raise StorageError(f"File type {ext or '(none)'} is not allowed")
    if not sniff_matches(ext, head):
        raise StorageError(f"File content does not match its {ext} extension")
    return ext, content_type


def validate_upload(filename: str, content: bytes) -> tuple[str, str]:
    """Validate an in-memory payload (small generated files only)."""
    return validate_upload_head(filename, content[:SNIFF_BYTES], len(content))


def measure(fileobj: IO[bytes]) -> int:
    """Size of a seekable file object, leaving the position at 0."""
    fileobj.seek(0, os.SEEK_END)
    size = fileobj.tell()
    fileobj.seek(0)
    return size


def safe_filename(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")) or "file"
    cleaned = "".join(c for c in base if c.isprintable() and c not in '"\r\n')
    return cleaned[:200] or "file"


def content_disposition(filename: str) -> str:
    fallback = safe_filename(filename).encode("ascii", "replace").decode().replace("?", "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(safe_filename(filename))}"


class StorageBackend(ABC):
    @abstractmethod
    async def save(self, key: str, fileobj: IO[bytes], content_type: str, *, max_bytes: int | None = None) -> int:
        """Stream ``fileobj`` into storage; returns bytes written."""

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        import io

        await self.save(key, io.BytesIO(content), content_type)

    @abstractmethod
    async def iter_chunks(self, key: str) -> AsyncIterator[bytes]:
        """Yield the stored object in chunks."""
        yield b""  # pragma: no cover

    async def get(self, key: str) -> bytes:
        """Read a whole object. Only for small generated files (tests, imports)."""
        parts = [chunk async for chunk in self.iter_chunks(key)]
        return b"".join(parts)

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def download_response(self, key: str, filename: str, content_type: str) -> Response: ...

    async def healthcheck(self) -> None:  # pragma: no cover - overridden where meaningful
        return None


class LocalStorageBackend(StorageBackend):
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = Path(key)
        if safe.is_absolute() or ".." in safe.parts or not key:
            raise StorageError("Invalid storage key")
        path = (self.base_dir / safe).resolve()
        if self.base_dir not in path.parents:
            raise StorageError("Invalid storage key")
        return path

    async def save(self, key: str, fileobj: IO[bytes], content_type: str, *, max_bytes: int | None = None) -> int:
        path = self._path(key)

        def _copy() -> int:
            path.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
            try:
                with os.fdopen(fd, "wb") as out:
                    while chunk := fileobj.read(CHUNK_SIZE):
                        written += len(chunk)
                        if max_bytes is not None and written > max_bytes:
                            raise UploadTooLarge("File exceeds the maximum upload size")
                        out.write(chunk)
                os.replace(tmp_name, path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
            return written

        return await asyncio.to_thread(_copy)

    async def iter_chunks(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        if not path.exists():
            raise StorageError("File not found")
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, CHUNK_SIZE):
                yield chunk
        finally:
            handle.close()

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    async def download_response(self, key: str, filename: str, content_type: str) -> Response:
        path = self._path(key)
        if not path.exists():
            raise StorageError("File not found")
        return FileResponse(
            path,
            media_type=content_type,
            headers={"Content-Disposition": content_disposition(filename)},
        )

    async def healthcheck(self) -> None:
        if not os.access(self.base_dir, os.W_OK):
            raise StorageError(f"Storage directory {self.base_dir} is not writable")


class S3StorageBackend(StorageBackend):
    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        settings = get_settings()
        self.bucket = settings.storage_bucket
        self.client: Any = boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint_url or None,
            aws_access_key_id=settings.storage_access_key or None,
            aws_secret_access_key=settings.storage_secret_key or None,
            region_name=settings.storage_region or None,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )
        self._bucket_checked = not settings.storage_auto_create_bucket

    async def _ensure_bucket(self) -> None:
        if self._bucket_checked:
            return
        from botocore.exceptions import ClientError

        def _check() -> None:
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError:
                self.client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(_check)
        self._bucket_checked = True

    async def save(self, key: str, fileobj: IO[bytes], content_type: str, *, max_bytes: int | None = None) -> int:
        await self._ensure_bucket()
        size = measure(fileobj)
        if max_bytes is not None and size > max_bytes:
            raise UploadTooLarge("File exceeds the maximum upload size")
        # upload_fileobj streams in multipart chunks; the body never sits in memory.
        await asyncio.to_thread(
            self.client.upload_fileobj,
            fileobj,
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return size

    async def iter_chunks(self, key: str) -> AsyncIterator[bytes]:
        from botocore.exceptions import ClientError

        await self._ensure_bucket()
        try:
            response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            raise StorageError("File not found") from exc
        body = response["Body"]
        chunks: Iterator[bytes] = body.iter_chunks(chunk_size=CHUNK_SIZE)
        sentinel = object()
        try:
            while True:
                chunk = await asyncio.to_thread(next, chunks, sentinel)
                if chunk is sentinel:
                    break
                yield chunk  # type: ignore[misc]
        finally:
            body.close()

    async def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        await self._ensure_bucket()
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    async def delete(self, key: str) -> None:
        await self._ensure_bucket()
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    async def download_response(self, key: str, filename: str, content_type: str) -> Response:
        if not await self.exists(key):
            raise StorageError("File not found")
        return StreamingResponse(
            self.iter_chunks(key),
            media_type=content_type,
            headers={"Content-Disposition": content_disposition(filename)},
        )

    async def healthcheck(self) -> None:
        await self._ensure_bucket()
        await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)


_backend: StorageBackend | None = None


def get_storage() -> StorageBackend:
    global _backend
    if _backend is None:
        settings = get_settings()
        if settings.storage_provider == "s3":
            _backend = S3StorageBackend()
        else:
            _backend = LocalStorageBackend(settings.local_storage_dir)
    return _backend


def generate_key(prefix: str, filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return f"{prefix}/{uuid.uuid4().hex}{ext}"


def reset_storage() -> None:
    global _backend
    _backend = None


def copy_stream(src: IO[bytes], dst: IO[bytes]) -> None:
    shutil.copyfileobj(src, dst, CHUNK_SIZE)
