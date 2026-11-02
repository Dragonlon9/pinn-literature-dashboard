"""跨进程刷新锁。

设计文档 §5.6 的 threading.Lock 只能约束同一进程；独立更新命令与 Streamlit
可能同时运行，因此再用一个操作系统文件锁保证“同一时刻只允许一个更新任务”。
"""

from __future__ import annotations

import os
from pathlib import Path


class InterProcessLock:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._handle = None
        self._locked = False

    def acquire(self) -> bool:
        if self._locked:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        self._locked = True
        return True

    def release(self) -> None:
        if not self._locked or self._handle is None:
            return
        handle = self._handle
        self._handle = None
        self._locked = False
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "InterProcessLock":
        if not self.acquire():
            raise TimeoutError("another update is already running")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
