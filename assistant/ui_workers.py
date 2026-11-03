"""Deliver thread launch failures through each window's existing result queue."""
import threading


def start_worker(target, on_failure, *, daemon=False):
    """Return whether a worker started; no retry and no UI work on its thread.

    on_failure runs synchronously on the calling UI thread. Callers enqueue one
    terminal result and keep their busy state until that result is consumed.
    A launch failure is not a failed model answer or a completed task.
    """
    try:
        threading.Thread(target=target, daemon=daemon).start()
    except Exception as exc:
        on_failure('背景工作未啟動：' + str(exc))
        return False
    return True
