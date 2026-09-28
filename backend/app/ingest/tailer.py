"""Log tailer for streaming lines from a growing log file.

Follows a log file like `tail -f`, handles file creation delays,
tracks byte offsets, decodes UTF-8 with replacement,
buffers partial lines until complete, survives rotation and truncation,
and optionally persists and resumes state.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import AsyncIterator, Optional, Tuple, Union

try:
    from backend.app.config import settings
except ImportError:
    try:
        from app.config import settings  # type: ignore
    except ImportError:
        settings = None

# Default poll interval from config settings if available, else 0.1s
DEFAULT_POLL_INTERVAL_S: float = (
    getattr(settings, "tailer_poll_interval_s", 0.1)
    if settings is not None
    else 0.1
)


def _open_shared_read(path: Path):
    """Open a file for reading in binary mode with shared read, write, and delete permissions.

    On Windows, standard open() denies deletion/renaming of an open file by other processes,
    causing PermissionError during log rotation. Using CreateFileW with FILE_SHARE_DELETE
    ensures seamless log rotation while the file is being actively tailed.
    """
    if os.name == "nt":
        import ctypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80

        handle = kernel32.CreateFileW(
            str(path),
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == -1 or handle == 0xFFFFFFFFFFFFFFFF:
            # Fall back to standard open if CreateFileW fails
            return open(path, "rb")

        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        return open(fd, "rb", closefd=True)
    else:
        return open(path, "rb")


def _get_file_id(stat_res: os.stat_result) -> Tuple[int, int]:
    """Extract a platform-robust unique file identifier (device, inode/ctime)."""
    ino = getattr(stat_res, "st_ino", 0)
    dev = getattr(stat_res, "st_dev", 0)
    if ino != 0:
        return (dev, ino)
    # Windows fallback when st_ino is 0: use high-resolution creation time
    ctime = getattr(stat_res, "st_ctime_ns", int(stat_res.st_ctime * 1e9))
    return (dev, ctime)


class LogTailer:
    """Follows a growing log file asynchronously, surviving rotation and truncation."""

    def __init__(
        self,
        path: Union[str, Path],
        state_path: Optional[Union[str, Path]] = None,
        poll_interval_s: Optional[float] = None,
        from_beginning: bool = False,
        state_save_interval_s: float = 2.0,
    ):
        self.path = Path(path).resolve()
        self.state_path = Path(state_path).resolve() if state_path else None
        self.poll_interval_s = (
            poll_interval_s if poll_interval_s is not None else DEFAULT_POLL_INTERVAL_S
        )
        self.from_beginning = from_beginning
        self.state_save_interval_s = state_save_interval_s

        self.offset: int = 0
        self.file_id: Optional[Tuple[int, int]] = None
        self._running: bool = True
        self._last_state_save: float = 0.0

    def stop(self) -> None:
        """Signal the lines generator loop to exit cleanly."""
        self._running = False
        self._save_state(force=True)

    def _load_state(self) -> Optional[dict]:
        """Load persisted state dictionary if present and valid."""
        if self.state_path and self.state_path.exists():
            try:
                content = self.state_path.read_text(encoding="utf-8")
                return json.loads(content)
            except Exception:
                return None
        return None

    def _save_state(self, force: bool = False) -> None:
        """Persist current offset and file identifier atomically to disk."""
        if not self.state_path or self.file_id is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_state_save) < self.state_save_interval_s:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
            data = {
                "inode": self.file_id[1],
                "dev": self.file_id[0],
                "offset": self.offset,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            tmp_path.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp_path, self.state_path)
            self._last_state_save = now
        except Exception:
            pass

    async def lines(self) -> AsyncIterator[str]:
        """Yield raw physical lines without trailing newlines as they are written."""
        # 1. Wait for file to exist
        while self._running and not self.path.exists():
            await asyncio.sleep(self.poll_interval_s)

        if not self._running:
            return

        # 2. Check if we need to resume from an old rotated file first (.1)
        state = self._load_state()
        if state is not None and self.state_path:
            saved_inode = state.get("inode")
            saved_offset = state.get("offset", 0)
            backup_path = self.path.with_name(f"{self.path.name}.1")
            if backup_path.exists() and saved_inode is not None:
                try:
                    b_stat = os.stat(backup_path)
                    b_id = _get_file_id(b_stat)
                    if b_id[1] == saved_inode and saved_offset < b_stat.st_size:
                        # Resume reading the rest of the rotated backup file
                        try:
                            bf = _open_shared_read(backup_path)
                            bf.seek(saved_offset, os.SEEK_SET)
                            self.file_id = b_id
                            self.offset = saved_offset
                            b_buf = bytearray()
                            while self._running:
                                chunk = bf.read(65536)
                                if not chunk:
                                    break
                                b_buf.extend(chunk)
                                while b"\n" in b_buf:
                                    n_idx = b_buf.index(b"\n")
                                    l_bytes = b_buf[:n_idx]
                                    del b_buf[:n_idx + 1]
                                    self.offset += n_idx + 1
                                    if l_bytes.endswith(b"\r"):
                                        l_bytes = l_bytes[:-1]
                                    self._save_state(force=False)
                                    yield l_bytes.decode("utf-8", errors="replace")
                            bf.close()
                            # Reset offset for the new active file
                            self.offset = 0
                            state = None  # Resumed completed old file, proceed to active file from 0
                        except OSError:
                            pass
                except OSError:
                    pass

        # 3. Open the active target file
        f = _open_shared_read(self.path)
        current_stat = os.fstat(f.fileno())
        self.file_id = _get_file_id(current_stat)

        # 4. Determine starting offset
        if state is not None:
            saved_inode = state.get("inode")
            saved_offset = state.get("offset", 0)
            current_size = current_stat.st_size
            if (saved_inode is None or saved_inode == self.file_id[1]) and saved_offset <= current_size:
                self.offset = saved_offset
                f.seek(self.offset, os.SEEK_SET)
            else:
                self.offset = 0
                f.seek(0, os.SEEK_SET)
        elif self.from_beginning:
            self.offset = 0
            f.seek(0, os.SEEK_SET)
        else:
            # Live mode without state: start at current end of file
            f.seek(0, os.SEEK_END)
            self.offset = f.tell()

        self._save_state(force=True)
        buffer = bytearray()

        try:
            while self._running:
                chunk = f.read(65536)
                if chunk:
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        newline_idx = buffer.index(b"\n")
                        line_bytes = buffer[:newline_idx]
                        del buffer[:newline_idx + 1]
                        self.offset += (newline_idx + 1)

                        if line_bytes.endswith(b"\r"):
                            line_bytes = line_bytes[:-1]

                        line_str = line_bytes.decode("utf-8", errors="replace")
                        self._save_state(force=False)
                        yield line_str
                else:
                    # EOF reached on current handle: inspect filesystem state
                    try:
                        stat_on_disk = os.stat(self.path)
                        disk_file_id = _get_file_id(stat_on_disk)
                    except OSError:
                        stat_on_disk = None
                        disk_file_id = None

                    if stat_on_disk is not None:
                        # Case A: Rotation detected (disk file ID differs from open file ID)
                        if disk_file_id != self.file_id:
                            # 1. Drain any remaining bytes from the old file handle
                            while True:
                                rem_chunk = f.read(65536)
                                if not rem_chunk:
                                    break
                                buffer.extend(rem_chunk)
                                while b"\n" in buffer:
                                    newline_idx = buffer.index(b"\n")
                                    line_bytes = buffer[:newline_idx]
                                    del buffer[:newline_idx + 1]
                                    self.offset += (newline_idx + 1)
                                    if line_bytes.endswith(b"\r"):
                                        line_bytes = line_bytes[:-1]
                                    self._save_state(force=False)
                                    yield line_bytes.decode("utf-8", errors="replace")

                            # If a trailing partial line was left without newline, emit it
                            if buffer:
                                line_str = buffer.decode("utf-8", errors="replace").rstrip("\r\n")
                                del buffer[:]
                                if line_str:
                                    yield line_str

                            # 2. Close old file handle
                            f.close()
                            self._save_state(force=True)

                            # 3. Open new file handle at offset 0
                            try:
                                f = _open_shared_read(self.path)
                                current_stat = os.fstat(f.fileno())
                                self.file_id = _get_file_id(current_stat)
                                self.offset = 0
                                f.seek(0, os.SEEK_SET)
                                self._save_state(force=True)
                            except OSError:
                                pass
                            continue

                        # Case B: Truncation detected (file size shrank below our offset)
                        if stat_on_disk.st_size < self.offset:
                            del buffer[:]
                            self.offset = 0
                            f.seek(0, os.SEEK_SET)
                            self._save_state(force=True)
                            continue

                    self._save_state(force=False)
                    await asyncio.sleep(self.poll_interval_s)

        finally:
            if f and not f.closed:
                f.close()
            self._save_state(force=True)
