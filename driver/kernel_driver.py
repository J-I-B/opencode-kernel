#!/usr/bin/env python3
"""Persistent Python kernel driver for opencode-kernel.

Pure stdlib, Python 3.8+ compatible. Speaks JSON-lines over stdin/stdout:

  startup:  {"ready": true, "pid": ..., "python": "..."}
  request:  {"id": str, "code": str}
  response: {"id": str, "ok": bool, "stdout": str, "stderr": str,
             "result": str|null, "error": str|null}

State lives in a persistent globals dict, so variables/imports/functions
survive across requests. SIGINT during execution surfaces as an Interrupted
response and the kernel keeps serving. EOF on stdin exits 0.
"""

import argparse
import ast
import contextlib
import io
import json
import os
import platform
import re
import sys
import traceback

RESULT_REPR_LIMIT = 2000
TRUNCATION_MARKER = (
    "\n[truncated: {total} chars total — data stays in kernel variables; "
    "print slices]"
)


def emit(obj):
    """Write exactly one protocol line to real stdout."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def diag(message):
    """Diagnostics go to real stderr, never stdout."""
    sys.stderr.write("[kernel-driver] " + message + "\n")
    sys.stderr.flush()


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARKER.format(total=len(text))


def recover_id(line):
    """Best-effort extraction of an id from a malformed JSON line."""
    match = re.search(r'"id"\s*:\s*("(?:[^"\\]|\\.)*")', line)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def respond(rid, ok, stdout, stderr, result, error):
    emit(
        {
            "id": rid,
            "ok": ok,
            "stdout": stdout,
            "stderr": stderr,
            "result": result,
            "error": error,
        }
    )


def handle_line(line, kernel_globals, max_output):
    line = line.strip()
    if not line:
        return

    try:
        request = json.loads(line)
    except ValueError:
        rid = recover_id(line)
        if rid is not None:
            respond(rid, False, "", "", None, "Malformed JSON request line")
        else:
            diag("malformed JSON request (no recoverable id): " + line[:200])
        return

    if not isinstance(request, dict) or not isinstance(request.get("id"), str):
        diag("request without a string id: " + line[:200])
        return
    rid = request["id"]

    code = request.get("code")
    if not isinstance(code, str):
        respond(rid, False, "", "", None, "request missing string field 'code'")
        return

    out = io.StringIO()
    err = io.StringIO()
    ok = True
    result = None
    error = None

    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            tree = ast.parse(code)
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                # Execute everything but the trailing expression, then eval it
                # so its value becomes the cell result.
                if len(tree.body) > 1:
                    head = ast.Module(body=tree.body[:-1], type_ignores=[])
                    exec(compile(head, "<kernel>", "exec"), kernel_globals)
                tail = ast.Expression(body=tree.body[-1].value)
                value = eval(compile(tail, "<kernel>", "eval"), kernel_globals)
                result = repr(value)[:RESULT_REPR_LIMIT]
            else:
                exec(compile(tree, "<kernel>", "exec"), kernel_globals)
    except KeyboardInterrupt:
        # Default SIGINT behavior raises KeyboardInterrupt in this (main)
        # thread. Report it, preserve globals, keep serving.
        ok = False
        error = "Interrupted (SIGINT); kernel state preserved"
    except SystemExit:
        # sys.exit() in user code must not kill the driver.
        ok = False
        error = traceback.format_exc() + "\n[SystemExit intercepted; kernel kept alive]"
    except Exception:
        ok = False
        error = traceback.format_exc()

    respond(
        rid,
        ok,
        truncate(out.getvalue(), max_output),
        truncate(err.getvalue(), max_output),
        result,
        error,
    )


def main():
    parser = argparse.ArgumentParser(description="opencode-kernel Python driver")
    parser.add_argument("--max-output", type=int, default=20000)
    args = parser.parse_args()
    max_output = max(0, args.max_output)

    kernel_globals = {"__name__": "__kernel__", "__builtins__": __builtins__}

    emit({"ready": True, "pid": os.getpid(), "python": platform.python_version()})

    for line in sys.stdin:
        try:
            handle_line(line, kernel_globals, max_output)
        except KeyboardInterrupt:
            # Interrupted between cells (e.g. while waiting on stdin);
            # keep serving.
            continue

    return 0


if __name__ == "__main__":
    sys.exit(main())
