#!/usr/bin/env python
"""
Phase 1 — Download every PDF in book_inventory.json.

Per book:  downloads/grade-{N}/{branch}/{book_code}.pdf
  * if the file already exists AND is >100KB  -> SKIPPED (not re-downloaded)
  * otherwise download with HTTP Range resume + retry (exponential backoff)
  * validate the file starts with b"%PDF" (and, when the server advertised a
    size, that we received the full byte count); on mismatch DELETE + FAILED
  * record download_status in state.json (resumable) and back into
    book_inventory.json; log every action to pipeline.log
Idempotent: re-running skips already-downloaded files. Stops early if the
running failure rate exceeds 10% (after a >=30-book sample).
"""

import os, re, json, time, threading, datetime, math, sys, subprocess
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
INV   = os.path.join(ROOT, "book_inventory.json")
STATE = os.path.join(ROOT, "state.json")
LOG   = os.path.join(ROOT, "pipeline.log")
DL    = os.path.join(ROOT, "downloads")
UA    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"

MAX_PASSES = 60            # curl -C - resume passes (resilient on the flaky TLS link)
PASS_TIMEOUT = 240        # per-pass curl --max-time
MAX_CONSEC_FAIL = 15      # give up this run after this many consecutive no-progress passes
WORKERS = 4
MIN_OK = 100 * 1024        # >100KB => treat as complete (skip)
FAIL_GATE = 0.10

lock = threading.Lock()
tls  = threading.local()

def _sess():
    if not hasattr(tls, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"})
        tls.s = s
    return tls.s

def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] [p1] {msg}"
    with lock:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line, flush=True)

def load_state():
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)

def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)

def expected_size(url):
    try:
        r = _sess().head(url, timeout=30, allow_redirects=True)
        return int(r.headers.get("Content-Length", 0)) or None
    except Exception:
        return None

def download_resumable(url, part, exp):
    """Download `url` to `part` using `curl -C -` resume passes (reliable on this
    flaky TLS link where python streams keep hitting IncompleteRead).
    Returns final byte size on success, None if still incomplete. The partial
    `.part` is KEPT on failure so the next run can resume."""
    if not os.path.exists(part):
        open(part, "wb").close()
    consec_fail = 0
    for i in range(1, MAX_PASSES + 1):
        cur = os.path.getsize(part)
        if exp and cur >= exp:
            return cur
        rc = subprocess.run(["curl", "-sL", "-A", UA, "--max-time", str(PASS_TIMEOUT),
                             "-C", "-", "--retry", "0", "-o", part, url],
                            timeout=PASS_TIMEOUT + 30).returncode
        new = os.path.getsize(part)
        if exp and new >= exp:
            return new                                  # complete
        if new > cur:
            consec_fail = 0                              # progress made
        else:
            consec_fail += 1                              # no progress (drop / refused)
            if consec_fail >= MAX_CONSEC_FAIL:
                break                                     # server unreachable; resume next run
            time.sleep(min(20, 2 ** consec_fail))         # exponential backoff on failures
    return os.path.getsize(part)                          # final size (may be < expected)

def process(inv):
    grade = inv["grade"]; branch = inv["branch"]; code = inv["book_code"]
    node = inv["node"]; url = inv["download_url"]
    dest_dir = os.path.join(DL, f"grade-{grade}", branch)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{code}.pdf")
    part = dest + ".part"

    # SKIP: already downloaded & big enough
    if os.path.exists(dest) and os.path.getsize(dest) > MIN_OK:
        return node, "skipped", os.path.getsize(dest)

    exp = expected_size(url)
    size = download_resumable(url, part, exp)
    complete = (exp is not None and size >= exp) or (exp is None and size > MIN_OK)
    if not complete:
        # incomplete: KEEP the partial .part for the next run; mark failed for now
        return node, "failed", size
    # complete: validate %PDF magic; only delete if it's corrupt
    with open(part, "rb") as f:
        magic = f.read(8)
    if not magic.startswith(b"%PDF"):
        os.remove(part)
        return node, "failed", 0
    os.replace(part, dest)
    return node, "success", os.path.getsize(dest)

def main():
    inv = json.load(open(INV, encoding="utf-8"))
    st = load_state()
    # apply prior download_status from state
    pending = []
    for b in inv:
        prev = st["books"].get(b["node"], {}).get("download_status")
        if prev in ("success", "skipped"):
            b["download_status"] = prev
            continue
        b.pop("download_status", None)
        pending.append(b)
    log(f"inventory: {len(inv)} books | already done: {len(inv)-len(pending)} | to process: {len(pending)}")

    counts = {"success": 0, "skipped": 0, "failed": 0}
    attempted = 0
    stop = False
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {}
        for b in pending:
            futs[ex.submit(process, b)] = b
        for i, fut in enumerate(as_completed(futs), 1):
            if stop:
                continue
            b = futs[fut]
            node = b["node"]
            try:
                _node, status, size = fut.result()
            except Exception as e:
                status, size = "failed", 0
                log(f"UNEXPECTED {node}: {e}")
            attempted += 1
            counts[status] += 1
            b["download_status"] = status
            with lock:
                st["books"].setdefault(node, {})["download_status"] = status
                st["books"][node]["dl_bytes"] = size
                save_state(st)
            if status == "failed":
                log(f"FAILED {node} (grade {b['grade']} {b['branch']} code {b['book_code']}) -> deleted/absent")
            elif i % 10 == 0:
                log(f"progress {i}/{len(pending)}: {counts}")
            # failure-rate gate (after a meaningful sample)
            if attempted >= 30:
                rate = counts["failed"] / attempted
                if rate > FAIL_GATE:
                    log(f"FAILURE GATE: {rate:.1%} > {FAIL_GATE:.0%} after {attempted} -> STOPPING")
                    stop = True
                    for f in futs:
                        f.cancel()

    # write download_status back into the inventory file
    for b in inv:
        b.setdefault("download_status", "pending")
    with open(INV, "w", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False, indent=1)
    log(f"download_status written back into book_inventory.json")

    # final summary (including previously-skipped)
    final = {"success": 0, "skipped": 0, "failed": 0, "pending": 0}
    for b in inv:
        final[b.get("download_status", "pending")] = final.get(b.get("download_status", "pending"), 0) + 1
    log(f"=== PHASE 1 SUMMARY === success={final['success']} skipped={final['skipped']} failed={final['failed']} pending={final['pending']}")
    present = 0
    for b in inv:
        p = os.path.join(DL, f"grade-{b['grade']}", b["branch"], f"{b['book_code']}.pdf")
        if os.path.exists(p):
            present += 1
    log(f"files physically present in downloads/: {present}/{len(inv)}")
    print(f"\nPHASE 1 RESULT: success={final['success']} skipped={final['skipped']} failed={final['failed']} pending={final['pending']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
