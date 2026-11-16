#!/usr/bin/env python3
"""Stdlib-only tests for driver/kernel_driver.py.

Run: python3 tests/test_driver.py
Prints PASS/FAIL per test; exits non-zero on any failure.
"""

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

DRIVER = Path(__file__).resolve().parent.parent / "driver" / "kernel_driver.py"


class Kernel:
    """Subprocess wrapper around the driver with timeout-aware reads."""

    def __init__(self, max_output=20000):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(DRIVER), "--max-output", str(max_output)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        self._n = 0
        ready = self.recv(10)
        assert ready.get("ready") is True, f"expected ready handshake, got: {ready!r}"
        assert isinstance(ready.get("pid"), int), f"ready handshake missing pid: {ready!r}"

    def recv(self, timeout):
        if not self.sel.select(timeout):
            raise TimeoutError(f"no driver response within {timeout}s")
        line = self.proc.stdout.readline()
        if not line:
            raise EOFError("driver closed stdout unexpectedly")
        return json.loads(line)

    def _write(self, code):
        self._n += 1
        rid = f"t{self._n}"
        self.proc.stdin.write(json.dumps({"id": rid, "code": code}) + "\n")
        self.proc.stdin.flush()
        return rid

    def exec(self, code, timeout=10):
        rid = self._write(code)
        resp = self.recv(timeout)
        assert resp.get("id") == rid, f"response id mismatch: expected {rid!r}, got {resp!r}"
        return resp

    def send_nowait(self, code):
        return self._write(code)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=5)
        finally:
            self.sel.close()
            for stream in (self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass


def test_persistence():
    k = Kernel()
    try:
        r1 = k.exec("x = 41")
        assert r1["ok"] is True, f"'x = 41' failed: {r1!r}"
        r2 = k.exec("x + 1")
        assert r2["ok"] is True, f"'x + 1' failed: {r2!r}"
        assert r2["result"] == "42", f"expected result '42', got {r2['result']!r}"
    finally:
        k.close()


def test_print_capture():
    k = Kernel()
    try:
        r = k.exec('print("hi")')
        assert r["ok"] is True, f"print cell failed: {r!r}"
        assert r["stdout"] == "hi\n", f"expected stdout 'hi\\n', got {r['stdout']!r}"
    finally:
        k.close()


def test_expr_vs_statement():
    k = Kernel()
    try:
        r1 = k.exec("2+3")
        assert r1["ok"] is True and r1["result"] == "5", f"expected result '5', got {r1!r}"
        r2 = k.exec("y = 7")
        assert r2["ok"] is True, f"'y = 7' failed: {r2!r}"
        assert r2["result"] is None, f"statement result should be null, got {r2['result']!r}"
    finally:
        k.close()


def test_error_preserves_state():
    k = Kernel()
    try:
        r1 = k.exec("z = 1")
        assert r1["ok"] is True, f"'z = 1' failed: {r1!r}"
        r2 = k.exec("raise ValueError('boom')")
        assert r2["ok"] is False, f"raise cell should fail: {r2!r}"
        assert r2["error"] and "ValueError" in r2["error"], f"traceback missing ValueError: {r2['error']!r}"
        assert "boom" in r2["error"], f"traceback missing message 'boom': {r2['error']!r}"
        r3 = k.exec("z")
        assert r3["ok"] is True and r3["result"] == "1", f"state lost after error: {r3!r}"
    finally:
        k.close()


def test_interrupt():
    k = Kernel()
    try:
        r1 = k.exec("z = 1")
        assert r1["ok"] is True, f"'z = 1' failed: {r1!r}"
        k.send_nowait("import time; time.sleep(30)")
        time.sleep(0.5)
        os.kill(k.proc.pid, signal.SIGINT)
        start = time.monotonic()
        r2 = k.recv(3)
        elapsed = time.monotonic() - start
        assert elapsed < 3, f"Interrupted response took {elapsed:.2f}s (>3s)"
        assert r2["ok"] is False, f"interrupted cell should be ok=false: {r2!r}"
        assert r2["error"] and "Interrupted" in r2["error"], f"expected Interrupted error, got {r2['error']!r}"
        r3 = k.exec("z")
        assert r3["ok"] is True and r3["result"] == "1", f"state lost after SIGINT: {r3!r}"
        assert k.proc.poll() is None, "driver died after SIGINT instead of continuing"
    finally:
        k.close()


def test_truncation():
    k = Kernel(max_output=1000)
    try:
        r = k.exec('print("a"*100000)')
        assert r["ok"] is True, f"big print failed: {r!r}"
        out = r["stdout"]
        assert len(out) < 100000, f"stdout not capped: {len(out)} chars"
        assert out.startswith("a" * 1000), "stdout should start with the first 1000 'a' chars"
        assert "[truncated:" in out, f"truncation marker missing: {out[-200:]!r}"
        assert "100001 chars total" in out, f"marker should report total chars: {out[-200:]!r}"
    finally:
        k.close()


TESTS = [
    test_persistence,
    test_print_capture,
    test_expr_vs_statement,
    test_error_preserves_state,
    test_interrupt,
    test_truncation,
]


def main():
    failures = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(TESTS) - failures}/{len(TESTS)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
