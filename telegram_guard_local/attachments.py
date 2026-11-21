"""Read approved attachment bytes without exposing guard state or following links."""
import asyncio
import hashlib
import os
import stat
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from .content import content_files
from .files import checked
from .model import GuardError

MAX_FILE_BYTES = 4 * 1024**3  # Telegram/account limits may be lower.


def attachment_path(value, protected_roots=()):
    path = Path(value)
    if not path.is_absolute() or "\x00" in value or ".." in path.parts:
        raise GuardError("attachment_requires_absolute_path")
    # No UNC/device paths, alternate streams, symlinks, junctions or hard links.
    if os.name == "nt" and (str(path).startswith("\\\\") or ":" in str(path)[2:]):
        raise GuardError("attachment_path_refused")
    path = path.absolute()
    parts = {p.casefold() for p in path.parts}
    name = path.name.casefold()
    if (parts & {"private", ".ssh", ".aws", ".azure", ".gnupg", ".kube"}
            or name == ".env" or name.startswith(".env.")
            or name.endswith((".session", ".session-journal", ".pem", ".key", ".pfx", ".p12"))
            or name in {"account.json", "credentials.json", "credentials", "whitelist.json"}):
        raise GuardError("protected_attachment")
    for root in protected_roots:
        if path == Path(root).absolute() or Path(root).absolute() in path.parents:
            raise GuardError("protected_attachment")
    checked(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GuardError("attachment_not_regular_file")
    if not 0 < info.st_size <= MAX_FILE_BYTES:
        raise GuardError("attachment_size_out_of_range")
    return path


def open_attachment(path, protected_roots=()):
    path = attachment_path(str(path), protected_roots)
    before = path.stat()
    stream = path.open("rb")
    try:
        current = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino) or current.st_nlink != 1:
            raise GuardError("attachment_changed")
        # Validate the actual opened handle as well as the requested pathname.
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            get_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            get_path.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = get_path(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
            if not 0 < length < len(buffer):
                raise GuardError("attachment_path_refused")
            actual = buffer.value
            if actual.startswith("\\\\?\\"):
                actual = actual[4:]
            if Path(actual) != path:
                raise GuardError("attachment_path_refused")
        elif Path("/proc/self/fd").exists():
            if Path(os.readlink(f"/proc/self/fd/{stream.fileno()}")) != path:
                raise GuardError("attachment_path_refused")
        attachment_path(str(path), protected_roots)
        return stream
    except BaseException:
        stream.close()
        raise


@dataclass
class PreparedFile:
    stream: object
    filename: str
    size: int
    sha256: str


@asynccontextmanager
async def prepare_attachments(content, protected_roots=()):
    prepared = {}
    try:
        for item in content_files(content):
            for field in ("path", "thumbnail"):
                value = item.get(field)
                if not value:
                    continue
                if value in prepared:
                    if field == "thumbnail" and prepared[value].size > 200 * 1024:
                        raise GuardError("attachment_size_out_of_range")
                    continue
                with open_attachment(value, protected_roots) as source:
                    before = os.fstat(source.fileno())
                    snapshot = tempfile.TemporaryFile(mode="w+b")
                    record = PreparedFile(snapshot, Path(value).name, 0, "")
                    prepared[value] = record
                    digest = hashlib.sha256()
                    while chunk := source.read(1024 * 1024):
                        record.size += len(chunk)
                        if record.size > MAX_FILE_BYTES or (field == "thumbnail" and record.size > 200 * 1024):
                            raise GuardError("attachment_size_out_of_range")
                        snapshot.write(chunk)
                        digest.update(chunk)
                        await asyncio.sleep(0)
                    after = os.fstat(source.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise GuardError("attachment_changed")
                    if not record.size:
                        raise GuardError("attachment_size_out_of_range")
                    record.sha256 = digest.hexdigest()
                    snapshot.seek(0)
        yield prepared
    except OSError:
        raise GuardError("attachment_unavailable") from None
    finally:
        for record in prepared.values():
            record.stream.close()


def validate_attachment_paths(content, protected_roots=()):
    try:
        for item in content_files(content):
            for field in ("path", "thumbnail"):
                if item.get(field):
                    attachment_path(item[field], protected_roots)
    except OSError:
        raise GuardError("attachment_unavailable") from None
