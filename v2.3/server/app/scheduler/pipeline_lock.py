"""Short DB transactions, long *file* mutex: never hold a SQLite write lock to compute.

All pipeline entry points on one SQLite host share this reentrant mutex. The OS
releases it on worker death; queued jobs do not need an unsafe expiring lease.
"""
from contextlib import contextmanager
from pathlib import Path
import threading

_registry_lock = threading.Lock()
_locks = {}
_local = threading.local()


class PipelineBusy(RuntimeError):
    pass


@contextmanager
def pipeline_mutex(session_factory):
    with session_factory() as session:
        engine = session.get_bind()
        database = engine.url.database
    path = (str(Path(database).resolve()) + ".pipeline.lock"
            if database and database != ":memory:" else None)
    key = path or str(id(engine))
    with _registry_lock:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(blocking=False):
        raise PipelineBusy("another pipeline is running")
    depths = getattr(_local, "depths", {})
    _local.depths = depths
    handle = None
    try:
        if not depths.get(key) and path:
            import fcntl  # server runtime: Linux/macOS, not the Windows client
            handle = open(path, "a+b")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PipelineBusy("another pipeline process is running") from exc
        depths[key] = depths.get(key, 0) + 1
        try:
            yield
        finally:
            depths[key] -= 1
    finally:
        if handle is not None:
            handle.close()
        lock.release()
