#!/usr/bin/env python
"""Targeted re-run: force TOC re-extraction for specific book codes.

Routes each book to the text-layer extractor (toc_extract) when its PDF has a
usable text layer, otherwise to the OCR extractor (p3_ocr). Updates
state.json + book_inventory.json toc_status, and removes the stale output
JSON when the re-run flags the book ocr_no_toc / no_toc_found (never keep
garbage output).

Usage:
    python rerun_books.py 110207 110212 110219
"""

import os, json, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import fitz
import toc_extract as te
import p3_ocr
from p2_extract import slug_for, dl_path, out_path, save

def main(codes):
    st = json.load(open(os.path.join(ROOT, "state.json"), encoding="utf-8"))
    books = json.load(open(os.path.join(ROOT, "book_inventory.json"), encoding="utf-8"))
    by_code = {str(b["book_code"]): b for b in books}
    for code in codes:
        b = by_code[code]
        node = str(b["node"])
        pdf = dl_path(b)
        if not (os.path.exists(pdf) and os.path.getsize(pdf) > 100*1024):
            print(f"{code} ({slug_for(b)}): no valid PDF, skipped"); continue
        d = fitz.open(pdf)
        use_text = te.has_text_layer(d)
        d.close()
        fp = out_path(b)
        if use_text:
            res = te.extract_toc_from_pdf(pdf, b["grade"], b["branch"],
                                           slug_for(b), code, fp)
        else:
            res = p3_ocr.extract_ocr(pdf, b["grade"], b["branch"],
                                     slug_for(b), code, fp)
        status = res.get("status")
        if status == "success":
            new = "success" if use_text else "ocr_success"
        else:   # no_toc_found / ocr_no_toc / ...
            new = "ocr_no_toc"
            if os.path.exists(fp):
                os.remove(fp)          # don't keep a garbage JSON around
        st["books"].setdefault(node, {})["toc_status"] = new
        b["toc_status"] = new
        save(st)
        json.dump(books, open(os.path.join(ROOT, "book_inventory.json"), "w",
                              encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"{code} ({slug_for(b)}): method={'text' if use_text else 'ocr'} "
              f"-> {new} | status={status} "
              f"ch={res.get('n_chapters')} les={res.get('n_lessons')} "
              f"toc_p{res.get('toc_page')}x{res.get('toc_pages')}")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["110207", "110212", "110219"]))
