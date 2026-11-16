#!/usr/bin/env python3
"""Verifier for the opencode-kernel cheap benchmark suite.

Usage:
    python3 verify.py <task_id> <workspace_dir>
    python3 verify.py --selftest <workspace_dir>

<workspace_dir> must contain ground_truth.json plus the task fixture
(repo/, server.log) and the task's output artifact:
    bulk-rename   -> the renamed repo/ itself
    log-analysis  -> answers.json
    repo-audit    -> audit.json

Prints PASS/FAIL with details; exit code 0 on PASS, 1 on FAIL.
--selftest fabricates correct artifacts from ground truth (expect PASS)
and one deliberately wrong artifact per task (expect FAIL), in a temp copy.
Python stdlib only, Python 3.8+ compatible.
"""

import json
import os
import random
import shutil
import sys
import tempfile

OLD_SYMBOL = "calc_total"
NEW_SYMBOL = "compute_total"
SENTINEL = "# sentinel: do-not-edit"
TASK_IDS = ("bulk-rename", "log-analysis", "repo-audit")
LOG_INT_KEYS = ("error_count", "checkout_mean_latency_ms", "timeout_unique_users")
LOG_STR_KEYS = ("top_error_code", "first_fatal_ts")


def load_ground_truth(ws):
    path = os.path.join(ws, "ground_truth.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def repo_files(ws):
    """Sorted (relpath, abspath) of every Python SOURCE file under repo/.

    Generated artifacts (__pycache__/*.pyc etc.) are excluded: the tasks
    concern source code, and bytecode left by an agent's compile/import
    checks is not a source occurrence. Amended after steered round 2, where
    a .pyc produced by the kernel arm's own py_compile verification was
    counted as a source file (disclosed in RESULTS.md).
    """
    repo = os.path.join(ws, "repo")
    out = []
    for root, _dirs, names in os.walk(repo):
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, ws).replace(os.sep, "/")
            out.append((rel, path))
    out.sort()
    return out


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def verify_bulk_rename(ws, gt):
    details = []
    ok = True
    files = repo_files(ws)
    if not files:
        return False, ["repo/ is missing or empty"]
    texts = {rel: read_text(path) for rel, path in files}

    stale = [rel for rel, text in texts.items() if OLD_SYMBOL in text]
    if stale:
        ok = False
        details.append("%d file(s) still contain %s: %s"
                       % (len(stale), OLD_SYMBOL, ", ".join(stale[:10])))
    else:
        details.append("no %s remains in repo/ (%d files scanned)"
                       % (OLD_SYMBOL, len(files)))

    expected = gt["rename"]
    total_new = sum(text.count(NEW_SYMBOL) for text in texts.values())
    files_new = sum(1 for text in texts.values() if NEW_SYMBOL in text)
    if total_new == expected["total_occurrences"]:
        details.append("%s occurrences: %d (expected %d)"
                       % (NEW_SYMBOL, total_new, expected["total_occurrences"]))
    else:
        ok = False
        details.append("%s occurrences: %d (expected %d)"
                       % (NEW_SYMBOL, total_new, expected["total_occurrences"]))
    if files_new == expected["files_containing"]:
        details.append("files containing %s: %d (expected %d)"
                       % (NEW_SYMBOL, files_new, expected["files_containing"]))
    else:
        ok = False
        details.append("files containing %s: %d (expected %d)"
                       % (NEW_SYMBOL, files_new, expected["files_containing"]))

    # Deterministic spot-check of sentinel lines in 3 sampled files.
    rels = [rel for rel, _path in files]
    sample = random.Random(0).sample(rels, min(3, len(rels)))
    for rel in sample:
        present = any(line.strip() == SENTINEL
                      for line in texts[rel].splitlines())
        if present:
            details.append("sentinel intact in %s" % rel)
        else:
            ok = False
            details.append("sentinel line missing in %s" % rel)
    return ok, details


def verify_log_analysis(ws, gt):
    path = os.path.join(ws, "answers.json")
    if not os.path.isfile(path):
        return False, ["answers.json is missing"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            answers = json.load(fh)
    except (ValueError, OSError) as exc:
        return False, ["answers.json is malformed: %s" % exc]
    if not isinstance(answers, dict):
        return False, ["answers.json must be a JSON object"]
    ok = True
    details = []
    expected = gt["log"]
    for key in LOG_INT_KEYS + LOG_STR_KEYS:
        want = expected[key]
        if key not in answers:
            ok = False
            details.append("%s: missing" % key)
            continue
        got = answers[key]
        want_type = int if key in LOG_INT_KEYS else str
        if type(got) is not want_type:
            ok = False
            details.append("%s: %r has wrong type (expected %s)"
                           % (key, got, want_type.__name__))
        elif got != want:
            ok = False
            details.append("%s: %r (expected %r)" % (key, got, want))
        else:
            details.append("%s: %r ok" % (key, got))
    return ok, details


def verify_repo_audit(ws, gt):
    path = os.path.join(ws, "audit.json")
    if not os.path.isfile(path):
        return False, ["audit.json is missing"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            audit = json.load(fh)
    except (ValueError, OSError) as exc:
        return False, ["audit.json is malformed: %s" % exc]
    if not isinstance(audit, list):
        return False, ["audit.json must be a JSON list"]
    got = set()
    for i, entry in enumerate(audit):
        if not isinstance(entry, dict):
            return False, ["audit.json entry %d is not an object" % i]
        if not isinstance(entry.get("file"), str) or \
                not isinstance(entry.get("name"), str) or \
                type(entry.get("lines")) is not int:
            return False, ["audit.json entry %d malformed: %r" % (i, entry)]
        got.add((entry["file"], entry["name"], entry["lines"]))
    want = {(e["file"], e["name"], e["lines"]) for e in gt["audit"]}
    missing = sorted(want - got)
    extra = sorted(got - want)
    details = ["audit entries: %d reported, %d expected" % (len(got), len(want))]
    if missing:
        details.append("missing entries: %s"
                       % "; ".join("%s:%s(%d)" % m for m in missing[:10]))
    if extra:
        details.append("unexpected entries: %s"
                       % "; ".join("%s:%s(%d)" % e for e in extra[:10]))
    return (not missing and not extra), details


VERIFIERS = {
    "bulk-rename": verify_bulk_rename,
    "log-analysis": verify_log_analysis,
    "repo-audit": verify_repo_audit,
}


def run_task(task_id, ws):
    try:
        gt = load_ground_truth(ws)
    except (OSError, ValueError) as exc:
        print("FAIL %s: cannot load ground_truth.json: %s" % (task_id, exc))
        return 1
    ok, details = VERIFIERS[task_id](ws, gt)
    print("%s %s" % ("PASS" if ok else "FAIL", task_id))
    for line in details:
        print("  - %s" % line)
    return 0 if ok else 1


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")


def selftest(ws):
    """Fabricate correct + deliberately wrong artifacts; check expectations."""
    gt = load_ground_truth(ws)
    tmp_root = tempfile.mkdtemp(prefix="bench-selftest-")
    copy = os.path.join(tmp_root, "ws")
    shutil.copytree(ws, copy)
    results = []

    def expect(label, task_id, want_ok):
        ok, details = VERIFIERS[task_id](copy, gt)
        good = ok == want_ok
        results.append(good)
        print("selftest %-28s expected %s, got %s -> %s"
              % (label, "PASS" if want_ok else "FAIL",
                 "PASS" if ok else "FAIL", "ok" if good else "MISMATCH"))
        if not good:
            for line in details:
                print("    %s" % line)

    try:
        # --- bulk-rename: correct = perform the rename everywhere ---
        for rel, path in repo_files(copy):
            text = read_text(path)
            if OLD_SYMBOL in text:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text.replace(OLD_SYMBOL, NEW_SYMBOL))
        expect("bulk-rename correct", "bulk-rename", True)
        # wrong: revert one occurrence in the first affected file
        for rel, path in repo_files(copy):
            text = read_text(path)
            if NEW_SYMBOL in text:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text.replace(NEW_SYMBOL, OLD_SYMBOL, 1))
                break
        expect("bulk-rename wrong", "bulk-rename", False)

        # --- log-analysis ---
        answers_path = os.path.join(copy, "answers.json")
        write_json(answers_path, gt["log"])
        expect("log-analysis correct", "log-analysis", True)
        bad = dict(gt["log"])
        bad["error_count"] = bad["error_count"] + 1
        write_json(answers_path, bad)
        expect("log-analysis wrong", "log-analysis", False)
        os.remove(answers_path)
        expect("log-analysis missing", "log-analysis", False)

        # --- repo-audit ---
        audit_path = os.path.join(copy, "audit.json")
        write_json(audit_path, gt["audit"])
        expect("repo-audit correct", "repo-audit", True)
        write_json(audit_path, gt["audit"][1:])  # drop one entry
        expect("repo-audit wrong", "repo-audit", False)
        os.remove(audit_path)
        expect("repo-audit missing", "repo-audit", False)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if all(results):
        print("SELFTEST PASS (%d checks)" % len(results))
        return 0
    print("SELFTEST FAIL (%d/%d checks ok)"
          % (sum(1 for r in results if r), len(results)))
    return 1


def main(argv):
    if len(argv) == 3 and argv[1] == "--selftest":
        return selftest(argv[2])
    if len(argv) == 3 and argv[1] in TASK_IDS:
        return run_task(argv[1], argv[2])
    print("usage: python3 verify.py <task_id> <workspace_dir>\n"
          "       python3 verify.py --selftest <workspace_dir>\n"
          "task ids: %s" % ", ".join(TASK_IDS), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
