#!/usr/bin/env python
"""
toc_extract.py — reusable TOC (فهرست) extraction for chap.sch.ir textbooks.

Phase 2 uses this on PDFs that HAVE a native text layer. Phase 3 (OCR) reuses
the line-reconstruction / classification / JSON-assembly helpers but feeds
lines from Tesseract hOCR coordinates instead.

Public API:
    has_text_layer(pdf, limit=15) -> bool
    extract_toc_from_pdf(pdf_path, grade, branch, subject, book_code,
                         out_path) -> dict   (status + data)

Design notes (from the grade-10/ensani pilot):
  * RTL Persian text in these InDesign PDFs is stored in visual order, so
    get_text('text') is bidi-scrambled. Logical lines are reconstructed from
    get_text('words'): group words by PyMuPDF's line, sort right-to-left
    (descending x0), re-join. Each TOC line is  [<page>] [<title>].
  * Normalisation: ي->ی, ك->ک, Arabic-Indic & ASCII digits -> Persian,
    rejoin ZWNJ-split words, tidy spaced parens, drop space before ':',
    collapse Unicode spaces (incl. U+200A hair-space).
  * Classification: lines starting with فهرست-section keywords split into
    front-matter / chapters (فصل ...) / back-matter; everything between
    chapter headers is a lesson entry.
"""

import os, re, json, datetime
import fitz  # PyMuPDF
fitz.TOOLS.mupdf_display_errors(False)   # some PDFs have corrupt streams; MuPDF's
                                         # error callback can poison the process
                                         # (UnicodeEncodeError on surrogate-bearing
                                         # messages), so silence it and rely on the
                                         # defensive per-page helpers below.

TOC_KEYWORDS = ("فهرست", "فهرست مطالب", "محتويات", "محتویات", "سرفصل", "چینش")
FRONT_KW = ("پیشگفتار", "ستایش", "مقدمه")
BACK_KW  = ("نیایش", "واژه‌نامه", "واژه نامه", "کتاب‌نامه", "کتاب نامه",
            "پاسخنامه", "پیوست", "ضمیمه", "فهرست")
NUM_WORDS = {"یکم":1,"دوم":2,"سوم":3,"چهارم":4,"پنجم":5,"ششم":6,
             "هفتم":7,"هشتم":8,"نهم":9,"دهم":10,"یازدهم":11,"دوازدهم":12,
             "اول":1,"دو":2,"سه":3}

AR_D, PERS_D = "٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹"
_ASCII_D = "0123456789"
_ZWJ = "\u200c"
_WS = r"[ \t\u00a0\u2000-\u200b\u202f\u3000]+"      # incl. U+200A hair-space, not ZWNJ

def normalize(s):
    s = s.replace("\u064a","\u06cc").replace("\u06aa","\u06a9")
    for a,p in zip(AR_D,PERS_D): s=s.replace(a,p)
    for a,p in zip(_ASCII_D,PERS_D): s=s.replace(a,p)
    s = re.sub(rf"\s*{_ZWJ}\s*", _ZWJ, s)
    s = re.sub(r"\(\s+","(",s); s = re.sub(r"\s+\)",")",s)
    s = re.sub(_WS," ",s)
    s = re.sub(r"\s+:",":",s)
    s = s.replace(" !","!").strip()
    s = re.sub(_WS," ",s).strip()
    return s

def has_text_layer(doc, limit=15):
    """True if ANY of the first `limit` pages carries a non-trivial text layer."""
    for i in range(min(limit, doc.page_count)):
        if len(_page_text(doc[i]).strip()) > 40:
            return True
    return False

def _page_text(page):
    """page.get_text('text') that degrades to '' on a corrupt page instead of
    raising (some chap.sch.ir PDFs have broken content streams)."""
    try:
        return page.get_text("text")
    except Exception:
        return ""

def _page_words(page):
    try:
        return page.get_text("words")
    except Exception:
        return []

def _reconstruct_lines(page, skip_heading=("فهرست",)):
    words = _page_words(page)
    groups = {}
    for x0,y0,x1,y1,word,bl,ln,_ in words:
        groups.setdefault((bl,ln),[]).append((x0,y0,x1,word))
    out=[]
    for (bl,ln),ws in groups.items():
        if not ws: continue
        yc = sum(w[1] for w in ws)/len(ws)
        raw = " ".join(w[3] for w in sorted(ws, key=lambda t:-t[0]))
        norm = normalize(raw)
        # leading page number — after normalize all digits are Persian ۰-۹
        m = re.match(r"^([۰-۹]+)\s+(.*)$", norm)
        if m:
            pagenum = int(m.group(1).translate(str.maketrans(PERS_D,_ASCII_D)))
            title = m.group(2).strip()
            if not title:          # a pure page-number line (flat-layout TOC) -> skip
                continue
        else:
            pagenum, title = None, norm
        if any(title.startswith(h) for h in skip_heading):
            continue              # the فهرست heading itself
        if re.fullmatch(r"[۰-۹٠-٩0-9]+", title):
            continue              # a line that is only a page number
        if not title:
            continue
        out.append((pagenum,title,int(page.number),round(yc,1)))
    out.sort(key=lambda t:t[3])
    return out

_TOC_ENTRY_KW = re.compile(
    r"فصل|(?<!ت)درس|کارگاه|گنج.?حکمت|روان.?خوانی|شعر.?خوانی|ستایش|نیایش|واژه.?نامه|کتاب.?نامه")
_END_DIGIT = re.compile(r"[۰-۹٠-٩0-9]\.?$")

def _is_toc_entry(pagenum, title):
    """Phase B — line-level filter. A wide-read line is a genuine TOC entry
    (not preface/body prose) if it:
      * ends with a page number (a digit, optional trailing '.'), OR
      * contains a dot-leader run ("...."), OR
      * starts with a TOC keyword (فصل/درس/کارگاه/گنج حکمت/روان‌خوانی/
        شعرخوانی/ستایش/نیایش/واژه‌نامه/کتاب‌نامه) and is short (<80 chars).
    Everything else (long paragraphs, preface prose, chapter-opening content)
    is dropped.  This filters the LINES, not the pages."""
    t = title
    if _END_DIGIT.search(t):
        return True
    if "...." in t:
        return True
    if _TOC_ENTRY_KW.match(t) and len(t) < 80:
        return True
    return False

def chapter_number(title):
    m = re.match(r"^فصل\s+([^:،\s]+)", title)
    tok = m.group(1) if m else ""
    if tok in NUM_WORDS: return NUM_WORDS[tok]
    d = re.sub(r"[^۰-۹0-9]","",tok)
    if d: return int(d.translate(str.maketrans(PERS_D+_ASCII_D,_ASCII_D+_ASCII_D)))
    return None

_DIAC = "\u064b-\u065f\u0670"
def _nd(s):
    """strip Arabic diacritics/harakes for robust keyword matching"""
    return re.sub(f"[{_DIAC}]+", "", s)

def classify(lines):
    """lines: [(pagenum,title,pidx,y)] -> (chapters, front_matter, back_matter).
    If the TOC has no فصل (chapter) markers (flat list, common in primary books),
    a single implicit chapter titled 'محتویات' is synthesized from all entries."""
    chapters, front, back, cur = [], [], [], None
    flat, pre = [], []
    for pagenum,title,pidx,y in lines:
        clean = title.strip()
        nc = _nd(clean)
        if any(nc.startswith(_nd(k)) for k in FRONT_KW):
            front.append({"title":clean}); continue
        if any(nc.startswith(_nd(k)) for k in BACK_KW):
            back.append({"title":clean}); continue
        if re.match(r"فصل", nc):
            cur = {"chapter_number":chapter_number(clean),"title":clean,"lessons":[]}
            chapters.append(cur); continue
        flat.append(clean)
        if cur is None:
            pre.append(clean)                       # before any chapter
        else:
            cur["lessons"].append({"lesson_number":len(cur["lessons"])+1,"title":clean})
    if not chapters and flat:
        chapters.append({"chapter_number":0,"title":"محتویات","lessons":
            [{"lesson_number":i+1,"title":t} for i,t in enumerate(flat)]})
    else:
        front.extend({"title":t} for t in pre)      # pre-chapter entries -> front matter
    return chapters, front, back

def extract_toc_from_pdf(pdf_path, grade, branch, subject, book_code, out_path):
    """Extract the TOC of a digital (text-layer) PDF. Returns a status dict."""
    doc = fitz.open(pdf_path)
    res = {"book_code":book_code,"grade":grade,"branch":branch,"subject":subject,
           "pdf":os.path.basename(pdf_path),"extraction_method":"text"}
    if not has_text_layer(doc, 15):
        res["status"]="no_text_layer"; doc.close(); return res
    # locate the TOC page(s) within the first 15 pages
    toc_start=None
    for i in range(min(15, doc.page_count)):
        if any(k in _page_text(doc[i]) for k in TOC_KEYWORDS):
            toc_start=i; break
    if toc_start is None:
        res["status"]="no_toc_found"; doc.close(); return res
    lines=[]
    toc_pages=0
    for off in range(6):                     # Phase A: read فهرst + next 5 pages
        idx=toc_start+off
        if idx>=doc.page_count: break
        page=doc[idx]
        pl=_reconstruct_lines(page)          # all lines, no per-page gate
        if off==0:
            lines.extend(pl); toc_pages+=1    # the فهرst page itself: unconditional
            continue
        # Phase B: keep only genuine TOC entries (line-level filter)
        pv=[l for l in pl if _is_toc_entry(l[0], l[1])]
        # Phase C: stop at the first continuation page with zero TOC entries
        if not pv:
            break
        lines.extend(pv); toc_pages+=1
    # de-dupe
    seen=set(); uniq=[]
    for it in lines:
        k=(it[0],it[1])
        if k in seen: continue
        seen.add(k); uniq.append(it)
    lines=uniq
    chapters,front,back = classify(lines)
    res.update({"status":"success",
                "grade":grade,"branch":branch,"subject":subject,"book_code":book_code,
                "toc_page":toc_start+1,"toc_pages":toc_pages,
                "chapters":chapters,
                "front_matter":front,"back_matter":back,
                "n_chapters":len(chapters),
                "n_lessons":sum(len(c["lessons"]) for c in chapters)})
    doc.close()
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out_path,"w",encoding="utf-8") as f:
        json.dump({k:res[k] for k in ("grade","branch","subject","book_code",
                                       "chapters","front_matter","back_matter",
                                       "toc_pages","extraction_method")},
                  f, ensure_ascii=False, indent=2)
    return res
