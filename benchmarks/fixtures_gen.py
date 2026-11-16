#!/usr/bin/env python3
"""Deterministic fixture generator for the opencode-kernel cheap benchmark suite.

Usage:
    python3 fixtures_gen.py <dest_dir>

Generates into <dest_dir>:
    repo/             60 Python files across 3 subpackages (pkg_alpha,
                      pkg_beta, pkg_gamma). Exactly 24 files contain the
                      symbol ``calc_total`` (definitions and/or call sites).
                      Exactly 7 functions span more than 30 lines
                      (ast end_lineno - lineno + 1); all others <= 30.
                      Every file carries a ``# sentinel: do-not-edit`` line.
    server.log        Exactly 20,000 log lines, deterministic mix.
    ground_truth.json Ground truth for all three benchmark tasks.

Everything is driven by a fixed seed; regenerating into the same destination
is idempotent (repo/ and server.log are wiped and recreated).
Python stdlib only, Python 3.8+ compatible.
"""

import ast
import json
import os
import random
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta

SEED = 20261110
PACKAGES = ["pkg_alpha", "pkg_beta", "pkg_gamma"]
FILES_PER_PKG = 20
SYMBOL = "calc_total"
SENTINEL = "# sentinel: do-not-edit"

# Files (even indices 0..14 in each package) contain calc_total.
CALC_INDICES = [0, 2, 4, 6, 8, 10, 12, 14]
# Of those, indices 0/2/4/6 hold the definition; 8/10/12/14 are call-only.
DEF_INDICES = {0, 2, 4, 6}

# Exactly 7 long functions: (package, file index) -> number of body
# statement lines. Span = def + docstring + rows=[] + k appends + return.
LONG_BODY_COUNTS = {
    ("pkg_alpha", 1): 31,
    ("pkg_alpha", 5): 34,
    ("pkg_beta", 3): 37,
    ("pkg_beta", 9): 40,
    ("pkg_beta", 17): 43,
    ("pkg_gamma", 7): 46,
    ("pkg_gamma", 13): 49,
}

SMALL_FUNCS = [
    '''def normalize_path(raw):
    """Normalize a request path."""
    cleaned = raw.strip()
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned.rstrip("/") or "/"''',
    '''def parse_header(header):
    """Split a header line into name and value."""
    name, _, value = header.partition(":")
    return name.strip().lower(), value.strip()''',
    '''def build_payload(user, route):
    """Build the outbound event payload."""
    payload = {"user": user, "route": route}
    payload["source"] = "edge"
    return payload''',
    '''def validate_entry(entry):
    """Return True when an entry has the required fields."""
    if "id" not in entry:
        return False
    if "price" not in entry or "qty" not in entry:
        return False
    return entry["qty"] >= 0''',
    '''def format_amount(cents):
    """Render an integer cent amount as a string."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return "%s%d.%02d" % (sign, whole, frac)''',
    '''def clamp(value, low, high):
    """Clamp value into the inclusive range [low, high]."""
    if value < low:
        return low
    if value > high:
        return high
    return value''',
    '''def merge_defaults(options, defaults):
    """Overlay user options on top of defaults."""
    merged = dict(defaults)
    for key in options:
        merged[key] = options[key]
    return merged''',
    '''def extract_id(token):
    """Extract the trailing numeric id from a token."""
    digits = "".join(ch for ch in token if ch.isdigit())
    return int(digits) if digits else 0''',
]

CALC_DEF = '''def calc_total(items, tax_rate=0.0):
    """Sum line items and apply tax."""
    subtotal = 0
    for item in items:
        subtotal += item["price"] * item["qty"]
    return round(subtotal * (1 + tax_rate), 2)'''

CALL_FORMS = [
    "    gross = calc_total(items)",
    "    net = calc_total(items, tax_rate=0.0)",
    "    taxed = calc_total(items, 0.08)",
    "    running += calc_total(items)",
    "    totals.append(calc_total(items))",
]

LOG_LINES = 20000
LEVELS = ["INFO", "WARN", "ERROR", "FATAL"]
LEVEL_WEIGHTS = [70, 18, 10, 2]
USERS = ["U%d" % (12000 + i) for i in range(40)]
ROUTES = ["/api/checkout", "/api/cart", "/api/search",
          "/api/login", "/api/health", "/api/orders"]
ROUTE_WEIGHTS = [20, 16, 16, 16, 16, 16]
ERROR_CODES = ["E408", "E512", "E204", "E317", "E101", "E523"]
ERROR_CODE_WEIGHTS = [10, 3, 2, 2, 1, 1]
MSGS = {
    "INFO": ["ok", "ok", "ok", "done", "miss", "auth ok", "queued"],
    "WARN": ["retry", "throttled", "slow query", "cache miss", "timeout"],
    "ERROR": ["bad input", "db timeout", "upstream timeout",
              "denied", "timeout", "failed"],
    "FATAL": ["panic", "oom", "db timeout", "corrupt state"],
}


def make_long_func(file_index, body_count):
    lines = [
        "def process_batch_%02d(records):" % file_index,
        '    """Flatten staged records into output rows."""',
        "    rows = []",
    ]
    for j in range(body_count):
        lines.append(
            '    rows.append({"index": %d, "state": "staged", "weight": %d})'
            % (j, j % 7))
    lines.append("    return rows")
    return "\n".join(lines)


def make_batch_summary(call_count):
    lines = [
        "def batch_summary(items):",
        '    """Compute summary totals for a line-item list."""',
        "    running = 0",
        "    totals = []",
    ]
    for j in range(call_count):
        lines.append(CALL_FORMS[j % len(CALL_FORMS)])
    lines.append("    return totals or [running]")
    return "\n".join(lines)


def build_repo_texts():
    """Return {relative_path: text} for all 60 repo files."""
    texts = {}
    calc_global_index = 0
    for pkg_idx, pkg in enumerate(PACKAGES):
        for fi in range(FILES_PER_PKG):
            rel = "repo/%s/mod_%02d.py" % (pkg, fi)
            is_calc = fi in CALC_INDICES
            has_def = is_calc and fi in DEF_INDICES
            blocks = ['"""Module mod_%02d of %s."""' % (fi, pkg)]
            if is_calc and not has_def:
                blocks.append("from .mod_00 import calc_total")
            blocks.append(SENTINEL)
            blocks.append("DEFAULT_LIMIT = %d" % (10 + fi))
            base = pkg_idx * FILES_PER_PKG + fi
            for j in range(3):
                blocks.append(SMALL_FUNCS[(base + j) % len(SMALL_FUNCS)])
            if has_def:
                blocks.append(CALC_DEF)
            if is_calc:
                # Target occurrences per calc file: 4, 5, 6 cycling.
                target = 4 + (calc_global_index % 3)
                calc_global_index += 1
                # One occurrence is the def (or the import line); the
                # rest are call sites inside batch_summary.
                blocks.append(make_batch_summary(target - 1))
            if (pkg, fi) in LONG_BODY_COUNTS:
                blocks.append(make_long_func(fi, LONG_BODY_COUNTS[(pkg, fi)]))
            texts[rel] = "\n\n".join(blocks) + "\n"
    return texts


def scan_repo(repo_dir):
    """Derive rename + audit ground truth from the written files."""
    files_containing = 0
    total_occurrences = 0
    audit = []
    for root, _dirs, names in os.walk(repo_dir):
        for name in sorted(names):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, os.path.dirname(repo_dir))
            rel = rel.replace(os.sep, "/")
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            n = text.count(SYMBOL)
            if n:
                files_containing += 1
                total_occurrences += n
            tree = ast.parse(text, filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    span = node.end_lineno - node.lineno + 1
                    if span > 30:
                        audit.append({"file": rel, "name": node.name,
                                      "lines": span})
    audit.sort(key=lambda e: (e["file"], e["name"]))
    return ({"files_containing": files_containing,
             "total_occurrences": total_occurrences}, audit)


def build_log_records():
    rng = random.Random(SEED)
    base_ts = datetime(2026, 11, 10, 0, 0, 0)
    records = []
    for i in range(LOG_LINES):
        level = rng.choices(LEVELS, weights=LEVEL_WEIGHTS)[0]
        user = rng.choice(USERS)
        route = rng.choices(ROUTES, weights=ROUTE_WEIGHTS)[0]
        if level in ("ERROR", "FATAL"):
            code = rng.choices(ERROR_CODES, weights=ERROR_CODE_WEIGHTS)[0]
        else:
            code = "E000"
        latency = rng.randint(5, 250)
        msg = rng.choice(MSGS[level])
        ts = (base_ts + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        records.append({"ts": ts, "level": level, "user": user,
                        "route": route, "code": code, "latency": latency,
                        "msg": msg})
    return records


def render_line(rec):
    return ('%s %s user=%s route=%s code=%s latency=%dms msg="%s"'
            % (rec["ts"], rec["level"], rec["user"], rec["route"],
               rec["code"], rec["latency"], rec["msg"]))


def log_ground_truth(records):
    error_count = sum(1 for r in records if r["level"] == "ERROR")
    code_counts = Counter(r["code"] for r in records
                          if r["level"] == "ERROR")
    ranked = code_counts.most_common()
    if not ranked:
        raise RuntimeError("no ERROR lines generated")
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        raise RuntimeError("top error code is tied; adjust seed/weights")
    top_error_code = ranked[0][0]

    checkout = [r["latency"] for r in records
                if r["route"] == "/api/checkout"]
    if not checkout:
        raise RuntimeError("no checkout lines generated")
    total, n = sum(checkout), len(checkout)
    # Avoid an exact .5 mean so half-up vs half-even rounding cannot differ.
    if (2 * total) % n == 0 and total % n != 0:
        for r in records:
            if r["route"] == "/api/checkout":
                r["latency"] += 1
                total += 1
                break
    checkout_mean = (2 * total + n) // (2 * n)  # round half up

    timeout_users = {r["user"] for r in records if "timeout" in r["msg"]}
    fatal = [r["ts"] for r in records if r["level"] == "FATAL"]
    if not fatal:
        raise RuntimeError("no FATAL lines generated")
    return {
        "error_count": error_count,
        "top_error_code": top_error_code,
        "checkout_mean_latency_ms": checkout_mean,
        "timeout_unique_users": len(timeout_users),
        "first_fatal_ts": fatal[0],
    }


def generate(dest):
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    repo_dir = os.path.join(dest, "repo")
    log_path = os.path.join(dest, "server.log")

    # Idempotent wipe/recreate of generated assets only.
    if os.path.isdir(repo_dir):
        shutil.rmtree(repo_dir)
    if os.path.exists(log_path):
        os.remove(log_path)

    texts = build_repo_texts()
    for rel in sorted(texts):
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(texts[rel])
    if len(texts) != 60:
        raise RuntimeError("expected 60 files, got %d" % len(texts))

    rename_gt, audit = scan_repo(repo_dir)
    if rename_gt["files_containing"] != 24:
        raise RuntimeError("expected 24 calc_total files, got %d"
                           % rename_gt["files_containing"])
    if len(audit) != 7:
        raise RuntimeError("expected 7 long functions, got %d" % len(audit))
    expected_long = {"repo/%s/mod_%02d.py" % (pkg, fi)
                     for pkg, fi in LONG_BODY_COUNTS}
    if {e["file"] for e in audit} != expected_long:
        raise RuntimeError("long functions landed in unexpected files")

    records = build_log_records()
    log_gt = log_ground_truth(records)  # may bump one latency to break ties
    with open(log_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(render_line(rec) + "\n")

    gt = {"rename": rename_gt, "audit": audit, "log": log_gt}
    gt_path = os.path.join(dest, "ground_truth.json")
    with open(gt_path, "w", encoding="utf-8") as fh:
        json.dump(gt, fh, indent=2)
        fh.write("\n")

    size = os.path.getsize(log_path) + os.path.getsize(gt_path)
    for root, _dirs, names in os.walk(repo_dir):
        for name in names:
            size += os.path.getsize(os.path.join(root, name))
    print("fixtures written to %s" % dest)
    print("total fixture size: %d bytes (%.2f MB)" % (size, size / 1e6))
    if size >= 2000000:
        raise RuntimeError("fixture size exceeds 2MB budget")
    print(json.dumps(gt, indent=2))
    return gt


def main(argv):
    if len(argv) != 2:
        print("usage: python3 fixtures_gen.py <dest_dir>", file=sys.stderr)
        return 2
    generate(argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
