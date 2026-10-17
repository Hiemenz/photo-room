#!/usr/bin/env python3
"""
One long-lived ``exiftool -stay_open`` process (or a small pool of them).

Spawning exiftool costs ~80-300 ms per call on a Pi because it is a Perl
script. Keeping it running and feeding it argument lists over a pipe turns
every metadata read or sidecar write into a round trip of a few milliseconds,
and batches of hundreds of files into one command.

``run(args, input=None)`` mirrors ``subprocess.run(["exiftool", *args])`` and
returns a ``CompletedProcess`` (text mode), so callers do not care which
mode is active. ``PERSISTENT = False`` (or a missing exiftool) falls back to
a plain subprocess per call, which is also what the test-suite's exiftool
doubles rely on.
"""

from __future__ import annotations

import atexit
import os
import queue
import select
import subprocess
import threading
import time
import uuid

PERSISTENT = True
POOL_SIZE = 2
DEFAULT_TIMEOUT = 120

_real_run = subprocess.run
_pool: "ExifPool | None" = None
_pool_lock = threading.Lock()


class ExifToolError(RuntimeError):
    pass


class ExifTool:
    """A single stay_open exiftool process. Not thread-safe; use the pool."""

    def __init__(self, exe: str = "exiftool"):
        self.exe = exe
        self.proc: subprocess.Popen | None = None
        self._err_q: "queue.Queue[bytes]" = queue.Queue()
        self._seq = 0
        self._start()

    def _start(self) -> None:
        self.proc = subprocess.Popen(
            [self.exe, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self._err_q = queue.Queue()
        threading.Thread(target=self._drain_err, args=(self.proc, self._err_q),
                         daemon=True, name="exiftool-stderr").start()

    @staticmethod
    def _drain_err(proc, q) -> None:
        try:
            for line in iter(proc.stderr.readline, b""):
                q.put(line)
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def execute(self, args: list[str], timeout: float = DEFAULT_TIMEOUT) -> tuple[bytes, str, int]:
        """Run one command; returns (stdout bytes, stderr text, exit status)."""
        if not self.alive():
            self._start()
        self._seq += 1
        token = str(uuid.uuid4().int % 10**9 * 1000 + self._seq % 1000)   # -execute takes digits only
        marker = f"{{ready{token}}}".encode()
        lines = ["-charset", "filename=UTF8", *args,
                 "-echo4", f"{{ready{token}}}${{status}}", f"-execute{token}"]
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
            out = self._read_until(marker, timeout)
        except (OSError, ExifToolError):
            self.kill()
            raise
        # stdout: <data>{readyTOKEN}\n
        data = out[: out.rfind(marker)]
        err_lines: list[str] = []
        status = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                line = self._err_q.get(timeout=0.5)
            except queue.Empty:
                continue
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text.startswith(marker.decode()):
                try:
                    status = int(text[len(marker):].strip() or 0)
                except ValueError:
                    status = 0
                break
            err_lines.append(text)
        return data, "\n".join(err_lines), status

    def _read_until(self, marker: bytes, timeout: float) -> bytes:
        fd = self.proc.stdout.fileno()
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ExifToolError("exiftool timed out")
            ready, _, _ = select.select([fd], [], [], min(left, 1.0))
            if not ready:
                if not self.alive():
                    raise ExifToolError("exiftool exited")
                continue
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise ExifToolError("exiftool closed its output")
            buf += chunk
            if marker in buf[-(len(marker) + 8):]:
                return bytes(buf)

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            proc.stdin.write(b"-stay_open\nFalse\n")
            proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:
            self.kill()
        self.proc = None


class ExifPool:
    def __init__(self, size: int = POOL_SIZE):
        self._free: "queue.Queue[ExifTool]" = queue.Queue()
        self._all: list[ExifTool] = []
        for _ in range(size):
            tool = ExifTool()
            self._all.append(tool)
            self._free.put(tool)

    def execute(self, args, timeout=DEFAULT_TIMEOUT):
        tool = self._free.get()
        try:
            return tool.execute(args, timeout)
        finally:
            self._free.put(tool)

    def close(self) -> None:
        for tool in self._all:
            tool.close()


def get_pool() -> ExifPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ExifPool()
        return _pool


def shutdown() -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool:
        pool.close()


atexit.register(shutdown)


def run(args: list[str], input: str | None = None, timeout: float = DEFAULT_TIMEOUT,
        binary: bool = False, files: list[str] | None = None) -> subprocess.CompletedProcess:
    """
    Run exiftool with ``args``. ``input`` is a newline-separated list of paths
    (what ``-@ -`` would read); ``files`` are plain trailing arguments.
    """
    args = [*args, *(files or [])]
    use_pool = PERSISTENT and subprocess.run is _real_run
    if not use_pool:
        cmd = ["exiftool", *args] + (["-@", "-"] if input is not None else [])
        return subprocess.run(cmd, input=input, capture_output=True, text=not binary, timeout=timeout)
    extra = [line for line in (input or "").split("\n") if line]
    try:
        out, err, status = get_pool().execute([*args, *extra], timeout)
    except (OSError, ExifToolError) as exc:
        return subprocess.CompletedProcess(["exiftool"], 255, stdout=b"" if binary else "", stderr=str(exc))
    return subprocess.CompletedProcess(
        ["exiftool"], status, stdout=out if binary else out.decode("utf-8", "replace"), stderr=err)
