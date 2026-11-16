#!/usr/bin/env python3
"""A/B benchmark runner for opencode-kernel (cheap suite).

Runs each task in two arms (kernel plugin enabled vs disabled) through
`opencode run --standalone` (private server, hermetic), captures per-step
cost/tokens from the JSON event stream, verifies output artifacts, and
enforces a cumulative cost guard (default $3, below the approved EUR 5 gate).

Usage: python3 run_bench.py [--out DIR] [--max-cost 3.0] [--tasks id,id]

Live model runs are serialized: one owner at a time. Fixtures are synthetic
and deterministic; no shared services or user config are touched.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = str(HERE.parent)  # opencode-kernel project root
MODEL = "deepseek/deepseek-flash"
RUN_ROOT = Path("/tmp/opencode/bench")
PER_RUN_TIMEOUT = 600

BASE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "providers": {"deepseek": {"settings": {"apiKey": "{env:DEEPSEEK_API_KEY}"}}},
    # Benchmark needs no MCP servers; disabling them keeps runs hermetic and
    # trims location boot time.
    "mcp": {
        "servers": {
            "exa": {"disabled": True},
            "chrome-devtools": {"disabled": True},
            "playwright": {"disabled": True},
        }
    },
    "agents": {
        "bench": {
            "description": "Hermetic benchmark agent",
            "mode": "primary",
            "model": MODEL,
            "system": "You are a coding agent. Complete the task directly with the available tools. Do not delegate to other agents. Be concise.",
            "steps": 40,
        }
    },
}


def make_config(arm):
    cfg = json.loads(json.dumps(BASE_CONFIG))
    if arm == "kernel":
        cfg["plugins"] = [{"package": PLUGIN, "options": {}}]
    return cfg


def run_one(task, arm, out_root):
    run_dir = RUN_ROOT / f"{task['id']}-{arm}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    subprocess.run(
        [sys.executable, str(HERE / "fixtures_gen.py"), str(run_dir)],
        check=True,
        capture_output=True,
    )
    (run_dir / ".opencode").mkdir()
    (run_dir / ".opencode" / "opencode.json").write_text(json.dumps(make_config(arm), indent=2))

    # Integrity: the agent must never see the answers during the run.
    truth = run_dir / "ground_truth.json"
    stash = RUN_ROOT / "truth" / f"{task['id']}-{arm}.json"
    stash.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(truth), str(stash))

    dest = out_root / task["id"] / arm
    dest.mkdir(parents=True, exist_ok=True)
    transcript_path = dest / "transcript.jsonl"
    cmd = [
        "opencode", "run", "--standalone", "--agent", "bench", "--model", MODEL,
        "--format", "json", "--archived", "--auto", task["prompt"],
    ]
    attempts = 0
    # OpenCode resolves the location from the PWD env var, not getcwd():
    # subprocess(cwd=...) alone leaves PWD stale and boots the wrong location.
    env = {**os.environ, "PWD": str(run_dir)}
    for attempt in range(6):
        attempts = attempt + 1
        start = time.time()
        with open(transcript_path, "w") as fh:
            proc = subprocess.run(
                cmd, cwd=run_dir, env=env, stdout=fh, stderr=subprocess.PIPE, text=True, timeout=PER_RUN_TIMEOUT
            )
        wall = time.time() - start
        head = transcript_path.read_text()[:500]
        # Safety net for transient boot failures (failed attempts cost $0).
        if proc.returncode != 0 and "Agent not found" in head and attempt < 5:
            time.sleep(min(2 ** attempt, 16))
            continue
        break

    # Restore ground truth for the verifier (was hidden from the agent).
    shutil.copy(str(stash), str(run_dir / "ground_truth.json"))

    cost = 0.0
    steps = 0
    tin = tout = treason = tcache_r = tcache_w = 0
    with open(transcript_path) as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "step_finish":
                p = ev.get("part", {})
                cost += p.get("cost") or 0
                steps += 1
                t = p.get("tokens") or {}
                tin += t.get("input", 0)
                tout += t.get("output", 0)
                treason += t.get("reasoning", 0)
                c = t.get("cache") or {}
                tcache_r += c.get("read", 0)
                tcache_w += c.get("write", 0)

    verify = subprocess.run(
        [sys.executable, str(HERE / "verify.py"), task["id"], str(run_dir)],
        capture_output=True,
        text=True,
    )
    metrics = {
        "task": task["id"],
        "arm": arm,
        "model": MODEL,
        "wall_seconds": round(wall, 1),
        "attempts": attempts,
        "steps": steps,
        "cost_usd": round(cost, 6),
        "tokens": {
            "input": tin,
            "output": tout,
            "reasoning": treason,
            "cache_read": tcache_r,
            "cache_write": tcache_w,
        },
        "run_exit": proc.returncode,
        "verify_exit": verify.returncode,
        "verify_pass": verify.returncode == 0,
        "verify_output": verify.stdout.strip()[-2000:],
        "stderr_tail": (proc.stderr or "")[-1000:],
    }
    (dest / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "runs"))
    ap.add_argument("--max-cost", type=float, default=3.0)
    ap.add_argument("--tasks", default="")
    args = ap.parse_args()

    tasks = json.loads((HERE / "tasks.json").read_text())
    if args.tasks:
        want = set(args.tasks.split(","))
        tasks = [t for t in tasks if t["id"] in want]
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    total = 0.0
    results = []
    for task in tasks:
        for arm in ("kernel", "baseline"):
            if total >= args.max_cost:
                print(
                    f"ABORT: cumulative cost ${total:.4f} >= guard ${args.max_cost:.2f}; remaining runs skipped",
                    flush=True,
                )
                results.append({"task": task["id"], "arm": arm, "aborted": True})
                continue
            print(f"RUN {task['id']} / {arm} ...", flush=True)
            try:
                m = run_one(task, arm, out_root)
            except subprocess.TimeoutExpired:
                m = {"task": task["id"], "arm": arm, "timeout": True, "verify_pass": False, "cost_usd": 0.0}
            total += m.get("cost_usd", 0.0)
            results.append(m)
            print(
                f"  steps={m.get('steps')} cost=${m.get('cost_usd', 0):.4f} "
                f"verify={'PASS' if m.get('verify_pass') else 'FAIL'} total=${total:.4f}",
                flush=True,
            )

    summary = {
        "model": MODEL,
        "total_cost_usd": round(total, 6),
        "cost_guard_usd": args.max_cost,
        "opencode_version": subprocess.run(
            ["opencode", "--version"], capture_output=True, text=True
        ).stdout.strip(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
