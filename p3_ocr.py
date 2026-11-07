#!/usr/bin/env python
"""
Phase 3 — OCR-based TOC extraction for SCANNED books (no native text layer).

For each book marked toc_status="needs_ocr":
  * render candidate TOC pages (physical pages ~4..10) to 300-DPI PNGs,
  * run Tesseract (fas) with hOCR output to get per-word coordinates,
  * reconstruct logical RTL lines (cluster by Y, sort X right-to-left),
  * classify into front-matter / chapters / back-matter (shared with Phase 2),
  * write output/.../{book_code} - {subject_name}.json with "extraction_method":"ocr".

Tesseract is invoked via subprocess against the installed binary, using the
project-local tessdata/ (Persian `fas`). Idempotent: skips books already
ocr_success.
"""

import os, re, json, glob, shutil, datetime, subprocess, tempfile, sys
import fitz
from bs4 import BeautifulSoup

ROOT     = os.path.dirname(os.path.abspath(__file__))
STATE    = os.path.join(ROOT, "state.json")
INV      = os.path.join(ROOT, "book_inventory.json")
LOG      = os.path.join(ROOT, "pipeline.log")
OUT      = os.path.join(ROOT, "output")
TESS     = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSDATA = os.path.join(ROOT, "tessdata")
DPI      = 300
PAGE_LO, PAGE_HI = 3, 9          # 0-based candidate TOC pages (4th..10th page)
OCR_WORKERS = 4
PSM      = "3"                  # fully automatic page segmentation (best for TOC pages)

import toc_extract as te
from p2_extract import slug_for, dl_path, out_path, save, load_old_stats, regression_check

def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] [p3] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)

def _ordered_lines(words, keep_trailing=False):
    """Rebuild lines preserving top-to-bottom order, each line RTL-joined.
    keep_trailing=True preserves trailing page numbers (needed by the
    wide-read line filter, which uses 'ends with a digit' as a signal)."""
    words.sort(key=lambda t: (t[1], -t[0]))
    lines, cur, cury = [], [words[0]], words[0][1]
    for w in words[1:]:
        if abs(w[1]-cury) <= 40:
            cur.append(w)
        else:
            lines.append(cur); cur=[w]; cury=w[1]
    if cur: lines.append(cur)
    res=[]
    for ln in lines:
        ln.sort(key=lambda t:-t[0])
        text=te.normalize(" ".join(w[3] for w in ln))
        text=re.sub(r"^[^ء-ي۰-۹٠-٩]+","",text).strip()      # drop leading OCR noise
        if not keep_trailing:
            text=re.sub(r"[۰-۹٠-٩0-9\s]+$","",text).strip() # drop trailing page-number garbage
        letters=sum(1 for ch in text if 0x0600<=ord(ch)<=0x06FF)
        if letters>=2: res.append(text)                       # keep only real text lines
    return res

def extract_ocr(pdf_path, grade, branch, subject, book_code, out_path):
    doc = fitz.open(pdf_path)
    res = {"book_code":book_code,"grade":grade,"branch":branch,"subject":subject,
           "extraction_method":"ocr"}
    workdir = tempfile.mkdtemp(prefix="ocr_")
    try:
        # OCR all candidate pages up-front and cache their lines
        hi = min(PAGE_HI + 1, doc.page_count)
        page_lines = {p: _ocr_lines(doc, p, workdir, keep_trailing=True)
                      for p in range(PAGE_LO, hi)}
        # Page selection: ONLY a page that shows the فهرست/فهرست-مطالب heading itself
        # is a TOC page. Pages carrying a heading-level پیشگفتار / «سخنی با
        # دانش‌آموزان» mark are prefaces and are skipped. محتویات alone no longer
        # qualifies (a preface can carry its own «محتویات» list). If no فهرst page
        # is found the book is flagged ocr_no_toc instead of dumping preface prose
        # as fake lessons.
        def _fahrest(ls):
            return any(("فهرست" in t) and len(t) <= 14 for t in ls)

        def _preface_mark(ls):
            for t in ls:
                tt = t.replace(te._ZWJ, "").replace("\u0640", "")
                if len(tt) <= 14 and ("پیشگفتار" in tt or "پیش گفتار" in tt
                                      or "سخنی با دانش" in tt):
                    return True
            return False

        def _toc_heading_line(t):
            nd = te._nd(t)
            return len(nd) <= 14 and ("فهرس" in nd or nd.startswith("محتویات")
                                      or nd.startswith("محتويات"))

        toc_pages = [p for p in page_lines
                     if _fahrest(page_lines[p]) and not _preface_mark(page_lines[p])]
        if not toc_pages:
            res["status"] = "ocr_no_toc"      # no فهرst page -> don't dump a preface
            return res
        pidx = toc_pages[0]
        # Phase A: wide read — OCR pidx and up to 5 following pages unconditionally
        # Phase B: keep only genuine TOC entry lines (shared filter with the text layer)
        # Phase C: stop at the first continuation page that contributes zero entries
        # Phase D: stop (close the JSON) once map-legend/timeline junk appears
        # The فهرst page itself (off==0) is never filtered to zero lines; only its
        # فهرst heading line(s) are dropped.
        lines = []
        toc_pages = 0
        for off in range(6):
            p = pidx + off
            if p not in page_lines:
                break
            pl = [t for t in page_lines[p] if not _toc_heading_line(t)]
            if off == 0:
                pl_full = pl
                pl = te.cut_at_junk_str(pl)
                lines.extend(pl); toc_pages += 1
                if pl != pl_full:
                    break                      # junk under the فهرst list -> close
                continue
            pl_full = pl
            pl = te.cut_at_junk_str(pl)
            if te.page_starts_preface_str(pl):
                break                        # preface body opened -> فهرst is over
            pv = [t for t in pl if te._is_toc_entry(None, t) or te._keep_fragment(t)]
            if not pv:
                break
            lines.extend(pv); toc_pages += 1
            if pl != pl_full:
                break                          # junk figure under the entries -> close
        chapters, front, back = te.classify([ (None, t, pidx, i*10) for i,t in enumerate(lines) ])
        res.update({"status":"success","chapters":chapters,"front_matter":front,
                    "back_matter":back,"toc_page":pidx+1,"toc_pages":toc_pages,
                    "n_chapters":len(chapters),
                    "n_lessons":sum(len(c["lessons"]) for c in chapters)})
        d=os.path.dirname(out_path)
        if d: os.makedirs(d, exist_ok=True)
        with open(out_path,"w",encoding="utf-8") as f:
            json.dump({k:res[k] for k in ("grade","branch","subject","book_code",
                                           "chapters","front_matter","back_matter",
                                           "toc_pages","extraction_method")},
                      f, ensure_ascii=False, indent=2)
        return res
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        doc.close()

def _ocr_lines(doc, pidx, workdir, keep_trailing=False):
    try:
        pg = doc[pidx]
        pix = pg.get_pixmap(dpi=DPI)
    except Exception:
        return []                        # corrupt page -> no OCR lines for it
    safe = re.sub(r"[\\/:*?\"<>|]", "", os.path.basename(doc.name or "book"))
    img = os.path.join(workdir, f"p{pidx}_{safe}.png")
    pix.save(img)
    base = os.path.join(workdir, f"p{pidx}.hocr")
    subprocess.run([TESS, img, base, "-l","fas","--psm",PSM,"--tessdata-dir",TESSDATA,"hocr"],
                   capture_output=True, text=True, timeout=300)
    h = base + ".hocr"
    if not os.path.exists(h): return []
    soup = BeautifulSoup(open(h, encoding="utf-8", errors="replace"), "html.parser")
    words=[]
    for w in soup.find_all(class_="ocrx_word"):          # tesseract 5 uses <span>, not <div>
        txt=w.get_text()
        t=(w.get("title") or "").replace("bbox ","",1).split()
        if len(t)<4 or not txt.strip(): continue
        words.append((int(t[0]), int(t[1]), int(t[2]), txt))
    if not words: return []
    return _ordered_lines(words, keep_trailing)

def main():
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    st = json.load(open(STATE, encoding="utf-8"))
    books = json.load(open(INV, encoding="utf-8"))
    lock = threading.Lock()
    todo = []
    for b in books:
        node = b["node"]
        tstat = st["books"].get(node, {}).get("toc_status")
        if tstat in ("success", "ocr_success"):
            continue
        if tstat not in ("needs_ocr", None):
            continue
        pdf = dl_path(b)
        if not (os.path.exists(pdf) and os.path.getsize(pdf) > 100*1024):
            continue
        todo.append(b)
    log(f"Phase 3: {len(todo)} books need OCR (workers={OCR_WORKERS})")
    ocr_ok = ocr_no = 0
    old_stats = load_old_stats()
    reg = {"regressed": 0, "improved": 0, "lessons_only": 0}

    def work(b):
        node = b["node"]
        res = extract_ocr(dl_path(b), b["grade"], b["branch"],
                          slug_for(b), str(b["book_code"]), out_path(b))
        with lock:
            if res.get("status") == "success":
                b["toc_status"] = "ocr_success"
                st["books"].setdefault(node, {})["toc_status"] = "ocr_success"
                save(st)
                regression_check(str(b["book_code"]), res.get("n_chapters", 0),
                                 res.get("n_lessons", 0), old_stats, reg, log)
                log(f"{node} ({slug_for(b)}): OCR TOC OK "
                    f"({res.get('n_chapters')} ch/{res.get('n_lessons')} les, p{res.get('toc_page')})")
                return "ok"
            b["toc_status"] = "ocr_no_toc"
            st["books"].setdefault(node, {})["toc_status"] = "ocr_no_toc"
            save(st)
            log(f"{node} ({slug_for(b)}): OCR found no usable TOC")
            return "no"

    with ThreadPoolExecutor(max_workers=OCR_WORKERS) as ex:
        futs = [ex.submit(work, b) for b in todo]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            ocr_ok += (r == "ok"); ocr_no += (r == "no")
            if i % 10 == 0:
                log(f"progress {i}/{len(todo)}: ok={ocr_ok} no_toc={ocr_no}")
    json.dump(books, open(INV, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"=== PHASE 3 SUMMARY === ocr_success={ocr_ok} ocr_no_toc={ocr_no}")
    log(f"=== PHASE 3 REGRESSION REPORT (vs output_old) === "
        f"regressed={reg['regressed']} improved={reg['improved']} "
        f"lessons_only_regressed={reg['lessons_only']}")
    print(f"\nPHASE 3 RESULT: ocr_success={ocr_ok} ocr_no_toc={ocr_no}")
    print(f"PHASE 3 REGRESSION: regressed={reg['regressed']} improved={reg['improved']} "
          f"lessons_only={reg['lessons_only']}")
    return 0

if __name__=="__main__":
    sys.exit(main())
