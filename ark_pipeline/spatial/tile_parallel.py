"""Let large polygons borrow idle spatial worker slots for native tile work."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


class TileBudget:
    """One semaphore shared by polygon processes and their helper threads.

    The calling polygon must already hold one slot. Helpers borrow extra slots
    without blocking; when all polygon workers are busy the caller runs serially.
    Native GEOS/H3 calls release the GIL, and threads share the source geometry
    rather than copying a large polygon into additional processes.
    """

    def __init__(self, slots, workers: int):
        self.slots, self.workers = slots, workers
        self.peak_workers = 1

    def map(self, function, jobs):
        if self.workers <= 1:
            yield from map(function, jobs)
            return

        def borrowed(job):
            try:
                return function(job)
            finally:
                self.slots.release()

        pending = set()
        # Each queued result is one bounded grid tile, never a whole polygon.
        with ThreadPoolExecutor(max_workers=self.workers - 1) as pool:
            try:
                for job in jobs:
                    ready = {future for future in pending if future.done()}
                    pending.difference_update(ready)
                    for future in ready:
                        yield future.result()
                    if len(pending) < self.workers - 1 and self.slots.acquire(False):
                        try:
                            pending.add(pool.submit(borrowed, job))
                        except BaseException:
                            self.slots.release()
                            raise
                        self.peak_workers = max(self.peak_workers, len(pending) + 1)
                    else:
                        # The polygon's reserved slot also does useful work.
                        yield function(job)
                while pending:
                    ready, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in ready:
                        yield future.result()
            finally:
                # A cancelled queued job never enters borrowed() to release its
                # permit. Running jobs release theirs before the pool exits.
                for future in pending:
                    if future.cancel():
                        self.slots.release()
