"""Advisory file lock shared by every path that can place an order.

Two processes that independently decide to sell the same position will both
send a market sell. The second either fails on insufficient balance or dumps a
coin the ledger has already closed. The hourly trader (:14) and the fast exit
watcher (every 5 minutes) are scheduled to overlap once an hour, so this is a
certainty, not a hazard — hence a real lock rather than careful timing.

The two callers want opposite things when the lock is busy:

  * the trader WAITS. An hourly cycle that silently skipped would suspend
    entries, reconciliation, trailing and the circuit breaker for a full hour.
  * the watcher SKIPS. It runs again in five minutes, and whatever holds the
    lock is the hourly cycle, which does strictly more than the watcher does.

flock is released by the kernel when the process dies, so a crash mid-cycle
cannot wedge the bot the way a lock file checked with exists() would.
"""
import errno
import fcntl
import time
from contextlib import contextmanager

from . import config

LOCK_PATH = config.TRADES_DIR / "trader.lock"


@contextmanager
def held(wait_s=0.0, poll_s=0.5, path=None, sleep=time.sleep):
    """Context manager yielding True if the lock was taken, False if busy.

    Never raises on contention — the caller decides what busy means for it.
    """
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    deadline = time.monotonic() + wait_s
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    break
                sleep(poll_s)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
