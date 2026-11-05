#!/usr/bin/env python
"""
Phase 2 — Text-layer TOC extraction for the digital (non-scanned) books.

For every book already downloaded whose PDF has a native text layer, run
toc_extract.extract_toc_from_pdf and write output/grade-{N}/{branch}/{slug}.json.
Books without a text layer are marked toc_status="needs_ocr" for Phase 3.
Idempotent: re-running skips books already marked success/ocr_success.

Output filename: {book_code} - {subject_name}.json, e.g.
    110201 -> output/grade-10/riazi/110201 - فارسی (1).json
Windows-illegal characters are stripped from the subject name; Persian letters,
digits, parens and dashes are preserved.
"""

import os, re, json, datetime, sys, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
INV  = os.path.join(ROOT, "book_inventory.json")
STATE= os.path.join(ROOT, "state.json")
LOG  = os.path.join(ROOT, "pipeline.log")
OUT  = os.path.join(ROOT, "output")

def clean_filename(name):
    """Strip Windows-illegal characters; keep Persian letters/digits, parens, dashes."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip())
    name = re.sub(r"\s+", " ", name)
    return name or "book"

def slug_for(book):
    """Human-readable subject name (used for the JSON 'subject' field + log lines)."""
    return clean_filename(book.get("subject_name")) or str(book["book_code"])

def dl_path(book):
    return os.path.join(ROOT, "downloads", f"grade-{book['grade']}",
                       book["branch"], f"{book['book_code']}.pdf")

def out_path(book):
    return os.path.join(OUT, f"grade-{book['grade']}", book["branch"],
                       f"{book['book_code']} - {slug_for(book)}.json")

def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] [p2] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)

def main():
    import toc_extract as te
    st = json.load(open(STATE, encoding="utf-8"))
    books = json.load(open(INV, encoding="utf-8"))
    old_stats = load_old_stats()
    counters = {"regressed": 0, "improved": 0, "lessons_only": 0}
    done = 0; text_ok = 0; needs_ocr = 0; no_toc = 0; missing = 0
    for b in books:
        node = b["node"]
        tstat = st["books"].get(node, {}).get("toc_status")
        if tstat in ("success", "ocr_success"):
            done += 1; continue
        pdf = dl_path(b)
        if not os.path.exists(pdf) or os.path.getsize(pdf) < 100*1024:
            b["toc_status"] = "no_pdf"; st["books"].setdefault(node, {})["toc_status"] = "no_pdf"
            missing += 1; log(f"{node}: no valid PDF -> skip (toc_status=no_pdf)"); save(st); continue
        res = te.extract_toc_from_pdf(pdf, b["grade"], b["branch"],
                                       slug_for(b), str(b["book_code"]), out_path(b))
        status = res.get("status")
        if status == "success":
            b["toc_status"] = "success"; text_ok += 1
            regression_check(str(b["book_code"]), res.get("n_chapters", 0),
                             res.get("n_lessons", 0), old_stats, counters)
            log(f"{node} ({slug_for(b)}): TOC OK via text layer "
                f"({res.get('n_chapters')} ch / {res.get('n_lessons')} lessons, p{res.get('toc_page')})")
        elif status == "no_text_layer":
            b["toc_status"] = "needs_ocr"; needs_ocr += 1
            log(f"{node} ({slug_for(b)}): no text layer -> needs OCR")
        elif status == "no_toc_found":
            b["toc_status"] = "needs_ocr"; no_toc += 1
            log(f"{node} ({slug_for(b)}): TOC keyword not in text layer -> will try OCR")
        else:
            b["toc_status"] = "error"; log(f"{node}: unexpected status {status}")
        st["books"].setdefault(node, {})["toc_status"] = b["toc_status"]
        save(st)
    # write toc_status back into the inventory file too
    json.dump(books, open(INV, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"=== PHASE 2 SUMMARY === success={text_ok} needs_ocr={needs_ocr} "
        f"no_toc={no_toc} no_pdf={missing} already_done={done}")
    log(f"=== PHASE 2 REGRESSION REPORT (vs output_old) === "
        f"regressed={counters['regressed']} improved={counters['improved']} "
        f"lessons_only_regressed={counters['lessons_only']}")
    print(f"\nPHASE 2 RESULT: text-layer OK={text_ok} needs_OCR={needs_ocr} "
          f"no_toc={no_toc} no_pdf={missing} already_done={done}")
    print(f"PHASE 2 REGRESSION: regressed={counters['regressed']} "
          f"improved={counters['improved']} lessons_only={counters['lessons_only']}")
    return 0

def save(st):
    import time
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    for attempt in range(5):
        try:
            os.replace(tmp, STATE)
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("could not write " + STATE)

# ---------------- Phase D — regression check vs. the pre-fix baseline -------------
def load_old_stats():
    """(chapters, lessons) per book_code from output_old/ (the pre-fix run).
    Filenames there are {code}.json, with the legacy edition slugs
    farsi1/farsi3 for 110201/111201."""
    m = {}
    for fp in glob.glob(os.path.join(ROOT, "output_old", "grade-*", "*", "*.json")):
        stem = os.path.splitext(os.path.basename(fp))[0]
        code = (stem.split(" - ")[0] if " - " in stem
                else {"farsi1": "110201", "farsi3": "111201"}.get(stem, stem))
        try:
            d = json.load(open(fp, encoding="utf-8"))
            m[code] = (len(d.get("chapters", [])),
                       sum(len(c.get("lessons", [])) for c in d.get("chapters", [])))
        except Exception:
            pass
    return m

def regression_check(code, n_ch, n_les, old_stats, counters, log_fn=None):
    """Phase D: log books that lost (or gained) chapters/lessons vs. the
    output_old baseline. 'regressed' = lost chapters; 'improved' = gained.
    log_fn: the caller's logger (keeps the [p2]/[p3] prefix correct)."""
    old = old_stats.get(str(code))
    if not old:
        return
    o_ch, o_les = old
    fn = log_fn or log
    if n_ch < o_ch:
        counters["regressed"] += 1
        fn(f"[regression] {code}: {o_ch} -> {n_ch} chapters")
    elif n_ch > o_ch:
        counters["improved"] += 1
        fn(f"[improved] {code}: {o_ch} -> {n_ch} chapters")
    if n_ch >= o_ch and n_les < o_les:
        counters["lessons_only"] += 1
        fn(f"[regression:lessons] {code}: {o_les} -> {n_les} lessons")

if __name__ == "__main__":
    sys.exit(main())
