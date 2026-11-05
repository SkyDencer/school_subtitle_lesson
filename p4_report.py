#!/usr/bin/env python
"""
Phase 4 — Consolidate all extracted TOCs and produce the final report.

Outputs:
  * all_tocs.json      -> master index (per-book stats) + embedded TOC bodies
                          + exceptions list
  * PROJECT_SUMMARY.md -> human summary: totals, grade/branch breakdown,
                          OCR-extracted list (lower confidence), exceptions
"""

import os, json, glob, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state.json")
INV   = os.path.join(ROOT, "book_inventory.json")
OUT   = os.path.join(ROOT, "output")
LOG   = os.path.join(ROOT, "pipeline.log")

def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] [p4] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)

def main():
    st = json.load(open(STATE, encoding="utf-8"))
    books = json.load(open(INV, encoding="utf-8"))
    # index output JSONs by book_code
    toc_files = {}
    for fp in glob.glob(os.path.join(OUT, "grade-*", "*", "*.json")):
        try:
            d = json.load(open(fp, encoding="utf-8"))
            toc_files[str(d.get("book_code"))] = {"file": fp.replace(ROOT, ".").replace("\\","/"), "data": d}
        except Exception:
            pass
    entries, tocs, exceptions = [], {}, []
    method_count = {"text": 0, "ocr": 0}
    for b in books:
        code = str(b["book_code"]); node = b["node"]
        tstat = st["books"].get(node, {}).get("toc_status") or b.get("toc_status")
        dstat = st["books"].get(node, {}).get("download_status") or b.get("download_status")
        rec = {"book_code": code, "grade": b["grade"], "branch": b["branch"],
               "subject_name": b.get("subject_name"), "node": node,
               "shared_across": b.get("shared_across"),
               "download_status": dstat, "toc_status": tstat}
        if code in toc_files:
            data = toc_files[code]["data"]
            rec["extraction_method"] = data.get("extraction_method")
            rec["n_chapters"] = data.get("n_chapters", len(data.get("chapters", [])))
            rec["n_lessons"]  = data.get("n_lessons", sum(len(c.get("lessons",[])) for c in data.get("chapters",[])))
            rec["toc_file"] = toc_files[code]["file"]
            tocs[code] = data
            method_count[data.get("extraction_method","?")] = method_count.get(data.get("extraction_method","?"),0)+1
            if rec.get("extraction_method")=="text": method_count["text"]+=0  # already counted
        else:
            # explain why no TOC
            if dstat in ("failed",) or dstat is None:
                rec["exception"] = "pdf_not_downloaded"
            elif tstat == "needs_ocr":
                rec["exception"] = "scanned_not_yet_ocr"
            elif tstat in ("no_toc", "ocr_no_toc", "no_toc_found"):
                rec["exception"] = "no_toc_found"
            elif tstat == "no_pdf":
                rec["exception"] = "pdf_missing"
            else:
                rec["exception"] = f"unprocessed ({tstat})"
            exceptions.append(rec)
        entries.append(rec)

    extracted = [e for e in entries if e.get("toc_file")]
    ocr_books = [e for e in extracted if e.get("extraction_method")=="ocr"]
    text_books = [e for e in extracted if e.get("extraction_method")=="text"]

    by_grade, by_branch = {}, {}
    for e in entries:
        by_grade[e["grade"]] = by_grade.get(e["grade"],0)+1
        by_branch[(e["grade"],e["branch"])] = by_branch.get((e["grade"],e["branch"]),0)+1

    all_tocs = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "total_books": len(entries),
        "extracted": len(extracted),
        "text_extracted": len(text_books),
        "ocr_extracted": len(ocr_books),
        "failed_or_missing": len(entries)-len(extracted),
        "by_grade_total": by_grade,
        "extraction_methods": method_count,
        "books": entries,
        "exceptions": exceptions,
        "tocs": tocs,
    }
    with open(os.path.join(ROOT,"all_tocs.json"),"w",encoding="utf-8") as f:
        json.dump(all_tocs, f, ensure_ascii=False, indent=1)
    log(f"all_tocs.json written: {len(entries)} books, {len(extracted)} extracted "
        f"({len(text_books)} text / {len(ocr_books)} ocr), {len(exceptions)} exceptions")

    write_summary(all_tocs, by_branch, text_books, ocr_books, exceptions)
    return 0

def write_summary(at, by_branch, text_books, ocr_books, exceptions):
    L = []
    A = L.append
    A("# Project Summary — Iranian Textbook TOC Extraction")
    A("")
    A(f"Generated: {at['generated']}  ")
    A(f"Source: chap.sch.ir (academic year 1405-1406)  ")
    A(f"Scope: academic track, grades 1-12 (primary, secondary, 4 high-school branches)\n")
    A("## Totals")
    A(f"**{at['extracted']} out of {at['total_books']} books extracted. "
      f"{at['text_extracted']} via text layer, {at['ocr_extracted']} via OCR. "
      f"{at['failed_or_missing']} exceptions.**\n")
    A(f"- Books discovered / attempted: **{at['total_books']}**")
    A(f"- TOCs successfully extracted: **{at['extracted']}**")
    A(f"  - via native text layer: **{at['text_extracted']}**")
    A(f"  - via OCR (lower confidence): **{at['ocr_extracted']}**")
    A(f"- Failed / not extracted: **{at['failed_or_missing']}**\n")
    A("## Books per grade")
    for g in sorted(at["by_grade_total"]):
        A(f"- grade {g}: {at['by_grade_total'][g]} books")
    A("")
    A("## Books per (grade, branch)")
    for (g,br),n in sorted(by_branch.items(), key=lambda kv:(kv[0][0],kv[0][1])):
        A(f"- grade {g:>2} / {br:<10}: {n}")
    A("")
    A("## OCR-extracted TOCs (lower confidence — scanned books)")
    if ocr_books:
        for e in ocr_books:
            A(f"- code {e['book_code']} (g{e['grade']} {e['branch']}): {e.get('subject_name')} — {e.get('n_chapters')} ch / {e.get('n_lessons')} lessons")
    else:
        A("- (none)")
    A("")
    A("## Exceptions (no TOC / not extracted)")
    if exceptions:
        for e in exceptions:
            A(f"- code {e['book_code']} (g{e['grade']} {e['branch']}): {e.get('subject_name')} — {e.get('exception')}")
    else:
        A("- (none)")
    A("")
    open(os.path.join(ROOT,"PROJECT_SUMMARY.md"),"w",encoding="utf-8").write("\n".join(L))

if __name__ == "__main__":
    main()
