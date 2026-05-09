#!/usr/bin/env python3.13
"""Micro-benchmark for BitString hot paths.

Drives each scenario with seeded random lengths in [1, 128] so the
byte-aligned fast path doesn't dominate. Scenarios mirror the
operations the JTAG/MPSSE pipeline does per chunk:

* int → BitString construction (the most-called shape)
* bytes → BitString construction
* BitString → BitString clone
* concat: BitString + BitString
* concat: BitString + BitStringSlice (slice is what JtagMpsse hands out)
* int extraction (full + slice)
* bytes extraction (full + slice, with both byte-aligned and unaligned)

Run with no arguments to print per-op timings; ``--compare-with <ref>``
runs once on the current tree, then ``git stash``-es uncommitted
changes, checks out ``<ref>``, runs again, and restores everything.
"""

import argparse
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager


# Make the in-tree acrobe importable regardless of cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from acrobe.bitstring import BitString, BitStringSlice  # noqa: E402


# Scenario size knobs. Picked so each scenario runs for ~50-200 ms
# at the current implementation speed; tweak ITER if you've made
# things much faster/slower.
ITER = 50_000
SEED = 0xC0FFEE


# ------------------------------------------------------------------
# Input fixtures: pre-built lists of (value, length) pairs and
# pre-built BitStrings, so we measure only the operation under test
# and not the input generation. Lengths are uniform in [1, 128].
# ------------------------------------------------------------------

def _make_inputs():
    rng = random.Random(SEED)
    int_inputs = []
    bytes_inputs = []
    for _ in range(ITER):
        length = rng.randint(1, 128)
        value = rng.getrandbits(length)
        int_inputs.append((value, length))
        bytes_inputs.append(
            (value.to_bytes((length + 7) // 8, 'little'), length))

    # Pre-built BitStrings for concat/extract scenarios.
    bs_inputs = [BitString(v, l) for v, l in int_inputs]
    # Slice covers a random sub-range that is *not* always byte-aligned.
    slice_inputs = []
    for bs in bs_inputs:
        n = len(bs)
        if n < 2:
            slice_inputs.append(bs[:])
            continue
        b = rng.randint(0, n - 1)
        e = rng.randint(b + 1, n)
        slice_inputs.append(bs[b:e])

    return int_inputs, bytes_inputs, bs_inputs, slice_inputs


# ------------------------------------------------------------------
# Timing helper. Runs ``fn(item)`` over ``items`` exactly once,
# returning total seconds. We don't divide here; the caller decides
# how to report (per-op, throughput, etc.).
# ------------------------------------------------------------------

def _time(fn, items):
    t0 = time.perf_counter()
    for it in items:
        fn(it)
    return time.perf_counter() - t0


# ------------------------------------------------------------------
# Scenarios. Each returns (name, per_op_seconds).
# ------------------------------------------------------------------

def bench_init_int(int_inputs):
    def op(pair):
        v, l = pair
        BitString(v, l)
    elapsed = _time(op, int_inputs)
    return ("BitString(int, len)", elapsed / len(int_inputs))


def bench_init_bytes(bytes_inputs):
    def op(pair):
        b, l = pair
        BitString(b, l)
    elapsed = _time(op, bytes_inputs)
    return ("BitString(bytes, len)", elapsed / len(bytes_inputs))


def bench_init_clone(bs_inputs):
    def op(bs):
        BitString(bs)
    elapsed = _time(op, bs_inputs)
    return ("BitString(clone)", elapsed / len(bs_inputs))


def bench_concat_bs_bs(bs_inputs):
    pairs = list(zip(bs_inputs, bs_inputs[1:] + bs_inputs[:1]))
    def op(pair):
        a, b = pair
        a + b
    elapsed = _time(op, pairs)
    return ("BitString + BitString", elapsed / len(pairs))


def bench_concat_bs_slice(bs_inputs, slice_inputs):
    pairs = list(zip(bs_inputs, slice_inputs))
    def op(pair):
        a, s = pair
        a + s
    elapsed = _time(op, pairs)
    return ("BitString + Slice", elapsed / len(pairs))


def bench_int_full(bs_inputs):
    def op(bs):
        int(bs)
    elapsed = _time(op, bs_inputs)
    return ("int(BitString)", elapsed / len(bs_inputs))


def bench_int_slice(slice_inputs):
    def op(s):
        int(s)
    elapsed = _time(op, slice_inputs)
    return ("int(Slice)", elapsed / len(slice_inputs))


def bench_bytes_full(bs_inputs):
    def op(bs):
        bytes(bs)
    elapsed = _time(op, bs_inputs)
    return ("bytes(BitString)", elapsed / len(bs_inputs))


def bench_bytes_slice(slice_inputs):
    def op(s):
        bytes(s)
    elapsed = _time(op, slice_inputs)
    return ("bytes(Slice) [random]", elapsed / len(slice_inputs))


def bench_bytes_slice_aligned(bs_inputs):
    """Byte-aligned slice path — exercises BitStringSlice.data's
    aligned shortcut. Mirrors what JtagMpsse._emit_shift does."""
    rng = random.Random(SEED + 1)
    aligned_slices = []
    for bs in bs_inputs:
        n = len(bs)
        # Pick begin in {0, 8, 16, ...} ≤ n, end in {b+8, b+16, ...} ≤ n.
        max_b_byte = n // 8
        if max_b_byte == 0:
            aligned_slices.append(bs[:])
            continue
        b = rng.randint(0, max_b_byte) * 8
        max_e_byte = n // 8
        if (b // 8) >= max_e_byte:
            aligned_slices.append(bs[b:b])
            continue
        e = rng.randint(b // 8 + 1, max_e_byte) * 8
        aligned_slices.append(bs[b:e])
    def op(s):
        bytes(s)
    elapsed = _time(op, aligned_slices)
    return ("bytes(Slice) [aligned]", elapsed / len(aligned_slices))


SCENARIOS = [
    ("init.int",            lambda f: bench_init_int(f["int"])),
    ("init.bytes",          lambda f: bench_init_bytes(f["bytes"])),
    ("init.clone",          lambda f: bench_init_clone(f["bs"])),
    ("concat.bs_bs",        lambda f: bench_concat_bs_bs(f["bs"])),
    ("concat.bs_slice",     lambda f: bench_concat_bs_slice(f["bs"], f["slice"])),
    ("extract.int_full",    lambda f: bench_int_full(f["bs"])),
    ("extract.int_slice",   lambda f: bench_int_slice(f["slice"])),
    ("extract.bytes_full",  lambda f: bench_bytes_full(f["bs"])),
    ("extract.bytes_slice", lambda f: bench_bytes_slice(f["slice"])),
    ("extract.bytes_aligned", lambda f: bench_bytes_slice_aligned(f["bs"])),
]


def run_once():
    """Run every scenario once and return a dict {key: per_op_seconds}.
    Each scenario gets fresh inputs so the order doesn't bias caching."""
    int_in, bytes_in, bs_in, slice_in = _make_inputs()
    fixtures = {"int": int_in, "bytes": bytes_in, "bs": bs_in, "slice": slice_in}

    # Warm-up pass to settle CPU clocks / caches before the measured run.
    for _key, fn in SCENARIOS:
        fn(fixtures)

    results = {}
    for key, fn in SCENARIOS:
        # Each scenario picks its own subset of fixtures.
        name, per_op = fn(fixtures)
        results[key] = (name, per_op)
    return results


def fmt_us(seconds):
    return f"{seconds * 1e6:6.2f} µs"


def print_results(label, results):
    width = max(len(name) for name, _ in results.values())
    print(f"\n=== {label} ===")
    for key, (name, per_op) in results.items():
        print(f"  {name.ljust(width)}  {fmt_us(per_op)}/op")


# ------------------------------------------------------------------
# Comparison-against-ref helpers. Stash the working tree, switch
# branches, run again, then restore everything. Prints a side-by-side
# table with relative deltas.
# ------------------------------------------------------------------

@contextmanager
def _git_at(ref):
    """Temporarily check out ``ref`` (with uncommitted changes
    stashed) and restore on exit. The bench script itself is copied
    to a tmp path before the checkout so it stays runnable even on
    refs that pre-date the script's existence."""
    head = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT,
        text=True).strip()
    if head == "HEAD":
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    stash = subprocess.run(
        ["git", "stash", "push", "--include-untracked",
         "-m", "bench_bitstring autostash"],
        cwd=ROOT, capture_output=True, text=True)
    stashed = "No local changes" not in stash.stdout
    try:
        subprocess.check_call(["git", "checkout", ref], cwd=ROOT)
        yield
    finally:
        subprocess.check_call(["git", "checkout", head], cwd=ROOT)
        if stashed:
            subprocess.check_call(["git", "stash", "pop"], cwd=ROOT)


def run_in_subprocess(script_path):
    """Re-execute the (caller-supplied) bench script in a fresh
    interpreter so the just-swapped-in ``acrobe.bitstring`` source
    actually loads. ``script_path`` is the bench script — pass an
    out-of-tree copy when the working tree may not have the script."""
    out = subprocess.check_output(
        [sys.executable, script_path],
        cwd=ROOT, text=True)
    return out


def parse_subprocess_output(out):
    """Parse the printed table back into {key: seconds} for diffing."""
    results = {}
    in_block = False
    for line in out.splitlines():
        if line.startswith("=== "):
            in_block = True
            continue
        if not in_block:
            continue
        line = line.rstrip()
        if not line.strip():
            continue
        # Format: "  <name padded>  <us>.<us> µs/op"
        parts = line.rsplit("µs/op", 1)
        if len(parts) != 2:
            continue
        head = parts[0].rstrip()
        # Last whitespace-separated token is the µs value.
        toks = head.rsplit(None, 1)
        if len(toks) != 2:
            continue
        name, us = toks
        try:
            seconds = float(us) * 1e-6
        except ValueError:
            continue
        results[name.strip()] = seconds
    return results


def print_diff(name_to_head, name_to_ref, label_head, label_ref):
    """Side-by-side per-op timing with the HEAD-vs-ref speedup ratio.
    A ratio > 1.0× means HEAD is that many times faster than ref."""
    width = max(len(n) for n in name_to_head)
    print()
    print(f"  {'scenario'.ljust(width)}  {label_head:>14}  "
          f"{label_ref:>14}  speedup")
    for name, head in name_to_head.items():
        ref = name_to_ref.get(name)
        if ref is None:
            continue
        ratio = ref / head if head else float('inf')
        print(f"  {name.ljust(width)}  {fmt_us(head):>14}  {fmt_us(ref):>14}  "
              f"{ratio:5.2f}×")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare-with",
                    help="git ref to bench against; current tree is "
                         "labelled 'HEAD'.")
    args = ap.parse_args()

    if args.compare_with is None:
        results = run_once()
        print_results("HEAD", results)
        return

    # Run on current tree first.
    head_results = run_once()
    head_named = {name: per_op for _key, (name, per_op) in head_results.items()}

    # Stash the script outside the working tree so a checkout that
    # predates the script can still find it.
    with tempfile.TemporaryDirectory() as tmp:
        ext_script = os.path.join(tmp, "bench_bitstring.py")
        shutil.copy2(os.path.abspath(__file__), ext_script)
        with _git_at(args.compare_with):
            ref_out = run_in_subprocess(ext_script)
    ref_named = parse_subprocess_output(ref_out)

    print_results("HEAD", head_results)
    print(f"\n=== {args.compare_with} ===")
    width = max(len(n) for n in ref_named)
    for name, per_op in ref_named.items():
        print(f"  {name.ljust(width)}  {fmt_us(per_op)}/op")

    print(f"\n=== HEAD vs {args.compare_with} (>1× = HEAD faster) ===")
    print_diff(head_named, ref_named, "HEAD", args.compare_with)


if __name__ == "__main__":
    main()
