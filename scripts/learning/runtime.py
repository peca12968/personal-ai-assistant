"""Process-held advisory GPU lease; a leftover file never proves a live owner."""
import os
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def gpu_lease(path=None):
    path = Path(path) if path is not None else ROOT/'data/learning/gpu.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    # The OS lock is automatically released if the process dies. Do not unlink:
    # another process may already have an open handle to the same file.
    with path.open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise RuntimeError('Another local learning/inference process holds the GPU lease') from exc
        try:
            yield
        finally:
            stream.seek(0)
            release()
