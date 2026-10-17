"""
Row-strip parallelism for the per-pixel passes of the render pipeline.

Almost every stage of the develop pipeline (white balance, tone, saturation,
HSL, grading, the elementwise half of clarity/texture/dehaze/sharpen, the final
8-bit quantisation) treats each pixel independently. Running such a stage over
horizontal strips gives

* the same result bit for bit (a strip is just a smaller image), and
* two speed-ups at full resolution: each strip's temporaries stay in the CPU
  cache instead of streaming through RAM, and NumPy/OpenCV release the GIL so
  the strips run on all cores.

Stages that look at neighbouring pixels (blurs) are not strip-safe and are
handled by their callers.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

# Below this many pixels a single call is faster than the pool round trip.
MIN_PIXELS = 300_000
STRIP_ROWS = 64
# Measured on the Pi 5 while other jobs shared it: extra threads made NumPy passes
# slower (memory-bandwidth bound), so the default is serial; PHOTO_ROOM_THREADS=N opts in.
DEFAULT_THREADS = 1

_pool: ThreadPoolExecutor | None = None
_threads_override: int | None = None


def thread_count() -> int:
    if _threads_override is not None:
        return _threads_override
    env = os.environ.get("PHOTO_ROOM_THREADS")
    if not env:
        return DEFAULT_THREADS
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    try:
        n = len(os.sched_getaffinity(0))
    except AttributeError:
        n = os.cpu_count() or 1
    return max(1, min(8, n))


def set_threads(n: int | None) -> None:
    """Override the worker count (None restores the default). Used by benchmarks and tests."""
    global _pool, _threads_override
    _threads_override = n
    if _pool is not None:
        _pool.shutdown(wait=False)
        _pool = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=thread_count(), thread_name_prefix="render")
    return _pool


def strips(h: int, rows: int = STRIP_ROWS) -> list[tuple[int, int]]:
    return [(a, min(h, a + rows)) for a in range(0, h, rows)]


def map_rows(fn, img: np.ndarray, *extra: np.ndarray, out: np.ndarray | None = None,
             dtype=None, rows: int = STRIP_ROWS) -> np.ndarray:
    """
    ``fn(strip, *extra_strips)`` over horizontal strips of ``img``; returns the
    stitched result (allocated on the first strip's shape/dtype unless ``out``).

    ``fn`` must be a pure per-pixel function (it may return the strip itself,
    or a new array, but must not write to a strip of ``img`` it did not own).
    """
    h = img.shape[0]
    if img.shape[0] * img.shape[1] < MIN_PIXELS:
        res = fn(img, *extra)
        if out is not None:
            out[...] = res
            return out
        return res

    spans = strips(h, rows)
    first = fn(img[spans[0][0]:spans[0][1]], *(e[spans[0][0]:spans[0][1]] for e in extra))
    if out is None:
        out = np.empty((h,) + first.shape[1:], dtype=dtype or first.dtype)
    out[spans[0][0]:spans[0][1]] = first

    def work(span):
        a, b = span
        out[a:b] = fn(img[a:b], *(e[a:b] for e in extra))

    rest = spans[1:]
    if thread_count() == 1:
        for s in rest:
            work(s)
    else:
        list(_executor().map(work, rest))
    return out


def run_parallel(fns) -> list:
    """Run independent zero-argument callables on the pool; results in order."""
    fns = list(fns)
    if thread_count() == 1 or len(fns) == 1:
        return [f() for f in fns]
    return list(_executor().map(lambda f: f(), fns))


def for_rows(fn, h: int, w: int, rows: int = STRIP_ROWS) -> None:
    """``fn(a, b)`` for every row span of an ``h`` x ``w`` image, in parallel.

    ``fn`` reads and writes its own rows only (the caller owns the arrays).
    """
    if h * w < MIN_PIXELS or thread_count() == 1:
        if h * w < MIN_PIXELS:
            fn(0, h)
        else:
            for a, b in strips(h, rows):
                fn(a, b)
        return
    list(_executor().map(lambda s: fn(*s), strips(h, rows)))
