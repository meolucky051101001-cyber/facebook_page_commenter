"""OS-owned lock protecting one database before startup recovery runs."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def exclusive_database(path):
    lock_path = Path(os.path.normcase(str(Path(path).resolve())) + ".lock")
    handle = open(lock_path, "a+b")
    acquired = False
    try:
        if not lock_path.stat().st_size:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise RuntimeError("Một tiến trình Commenter khác đang sử dụng cơ sở dữ liệu này.") from None
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
