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

TOC_KEYWORDS = ("فهرس", "محتويات", "محتویات", "سرفصل", "چینش")
FRONT_KW = ("پیشگفتار", "ستایش", "مقدمه")
BACK_KW  = ("نیایش", "واژه‌نامه", "واژه نامه", "کتاب‌نامه", "کتاب نامه",
            "پاسخنامه", "پیوست", "ضمیمه", "فهرست", "منابع", "مآخذ")
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
    s = s.replace("\u0640","")              # tatweel (منـابع / نمـودارهای…)
    s = re.sub(rf"\s*{_ZWJ}\s*", _ZWJ, s)
    s = re.sub(rf"^{_ZWJ}+", "", s)         # leading ZWNJ (OCR artifact)
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
        # the فهرست/محتویات heading itself, incl. diacritic-laden spellings
        # (decorative Arabic headings like اَلْـفِـهـرِس)
        nd = _nd(title)
        if any(nd.startswith(h) for h in skip_heading) or \
           (len(nd) <= 14 and ("فهرس" in nd or nd.startswith("محتویات") or nd.startswith("محتويات"))):
            continue
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

def _is_timeline_junk(s):
    """True for bottom-of-page map-legend / timeline-number lines, e.g. 'م .۶۳۶',
    'ق . م .۲۶۴-۱۴۶' (میلادی / قبل‌از‌میلاد year ticks under a figure). The only
    letters allowed are م/ق, the rest are digits, dots, dashes and spaces."""
    t = s.strip()
    if not t or len(t) > 16:
        return False
    if not re.search(r"[۰-۹٠-٩0-9]", t):
        return False
    letters = re.sub(r"[۰-۹٠-٩0-9.\s\-–—:؛,،]+", "", t)
    return set(letters) <= {"م", "ق"}

def cut_at_junk(lines):
    """lines: [(pagenum,title,pidx,y)] in y-order. Return the list truncated at the
    first timeline-junk line (junk only occurs at the bottom of a TOC page, above a
    map/timeline figure)."""
    for i, (_pn, t, _pi, _y) in enumerate(lines):
        if _is_timeline_junk(t):
            return lines[:i]
    return lines

def cut_at_junk_str(lines):
    """String-line variant of cut_at_junk (OCR lines carry no pagenum/coords)."""
    for i, t in enumerate(lines):
        if _is_timeline_junk(t):
            return lines[:i]
    return lines

def clean_title(t):
    """Reduce a raw TOC fragment to a plain title: drop dot leaders, trailing page
    numbers, stuck digits, Arabic-ending ة/ۀ spellings (-> ه) and harakat/tatweel.
    Note: these text layers encode Persian ت as U+062A, so only U+0629 (ة, teh
    marbuta) and U+06C0 (ۀ) are rewritten to ه — ت (U+062A) is left untouched.
    (درجة->درجه, معادلة->معادله, ضابطة->ضابطه)."""
    t = t.strip()
    t = re.sub(r"\.{2,}", "", t)                                       # dot leaders
    t = re.sub(r"\s+[۰-۹٠-٩0-9][۰-۹٠-٩0-9.\s]*$", "", t)              # trailing page number
    t = re.sub(r"[۰-۹٠-٩0-9]+$", "", t)                                # stuck digit (کاربردها۲)
    t = t.replace("\u0629", "\u0647")   # ة (teh marbuta) -> ه
    t = t.replace("\u06c0", "\u0647")   # ۀ (heh with yeh) -> ه
    t = _nd(t)                                                          # harakat / tatweel
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()

def _is_toc_entry(pagenum, title):
    """Phase B — line-level filter. A wide-read line is a genuine TOC entry
    (not preface/body prose) if it:
      * ends with a page number (a digit, optional trailing '.'), OR
      * contains a dot-leader run ("...."), OR
      * starts with a TOC keyword (فصل/درس/کارگاه/گنج حکمت/روان‌خوانی/
        شعرخوانی/ستایش/نیایش/واژه‌نامه/کتاب‌نامه) and is short (<80 chars).
    Timeline/map-legend junk is always rejected. This filters the LINES, not the pages."""
    t = title
    if _is_timeline_junk(t):
        return False           # map legend / timeline numbers -> stop, don't keep
    if _END_DIGIT.search(t):
        return True
    if "...." in t:
        return True
    if _TOC_ENTRY_KW.match(t) and len(t) < 80:
        return True
    return False

def chapter_number(title):
    m = re.match(r"^فصل\s+([^:،\s]+)", title)
    tok = _nd(m.group(1)) if m else ""      # strip shadda etc.: اوّل -> اول
    if tok in NUM_WORDS: return NUM_WORDS[tok]
    d = re.sub(r"[^۰-۹0-9]","",tok)
    if d: return int(d.translate(str.maketrans(PERS_D+_ASCII_D,_ASCII_D+_ASCII_D)))
    return None

_DIAC = "\u064b-\u065f\u0670"
def _nd(s):
    """strip Arabic diacritics/harakes + tatweel for robust keyword matching
    (decorative headings spell the فهرس root as اَلْفِـهـرِس)."""
    return re.sub(f"[{_DIAC}\u0640]+", "", s)

# ---- hierarchy rebuild (flat TOCs whose chapter/lesson lines are split
# fragments, common in InDesign text layers and OCR line clusters) ----
_ORDINALS = ("اوّل","اول","دوم","سوم","چهارم","پنجم","ششم","هفتم","هشتم","نهم",
             "دهم","یازدهم","دوازدهم","دو","سه")
_ORD_RE = re.compile(r"^(" + "|".join(map(re.escape,_ORDINALS)) + r")(?=\s|$)")
_LABEL_RE = re.compile(r"^درس\s*([۰-۹٠-٩0-9]+)$")
_NUM_CHARS = PERS_D + "٠١٢٣٤٥٦٧٨٩" + _ASCII_D

def _num(s):
    return int(s.translate(str.maketrans(_NUM_CHARS, _ASCII_D*3)))

def _ordinal_of(title):
    """The ordinal word when the line is a bare chapter-ordinal fragment ('دوم',
    'اوّل'), else None."""
    s = _nd(title.strip())
    for o in _ORDINALS:
        if s == o:
            return o
    return None

def _label_num(title):
    """The lesson number when the line is a bare 'درس N' label fragment, else None."""
    m = _LABEL_RE.fullmatch(title.strip())
    return _num(m.group(1)) if m else None

def _is_noise(title):
    """A bare short word with no page number, dot leader or TOC keyword — running
    headers (ریاضی / آمار) or stray text at the top of a TOC page."""
    s = _nd(title)
    letters = re.sub(r"[^ء-ي]", "", s)
    return (not re.search(r"[۰-۹٠-٩0-9]", s) and "...." not in title
            and len(letters) < 8)

def _keep_fragment(title):
    """A line is worth passing on to classify() even without a page number / dots:
    bare chapter ordinals and bare 'درس N' labels are structural fragments."""
    return _ordinal_of(title) is not None or _label_num(title) is not None

# Heading-level preface markers. A فهرst page may LIST "پیشگفتار" as an entry
# (with dot leaders + page number) — only a standalone heading (no dots, no page
# number) means the preface body has started and the فهرst is over.
_PREFERENCE_HEADINGS = ("پیشگفتار", "پیش گفتار", "پیش‌گفتار", "معارف", "ستایش",
                        "سخن با", "سخنی با")

def _is_preface_heading(title):
    """True for a standalone preface heading (not a فهرst entry with leaders/page)."""
    t = title.strip()
    if "...." in t:
        return False
    if _END_DIGIT.search(t):
        return False
    return any(t.startswith(h) for h in _PREFERENCE_HEADINGS)

def page_starts_preface(lines):
    """lines: [(pn,title,pidx,y)] in y-order. True when the page's topmost line is
    a preface heading — the فهرst has ended and the preface body has started."""
    return bool(lines) and _is_preface_heading(lines[0][1])

def page_starts_preface_str(lines):
    """String-line variant of page_starts_preface (OCR lines, already y-ordered)."""
    return bool(lines) and _is_preface_heading(lines[0])

def classify(lines):
    """lines: [(pagenum,title,pidx,y)] -> (chapters, front_matter, back_matter).

    Rules, in order, per y-sorted line:
      * FRONT_KW / BACK_KW keyword lines -> front / back matter
      * timeline-junk fragments -> skipped
      * "فصل ..." -> chapter
      * ordinal-led line ("سوم کار با داده‌ها …", or a bare ordinal that pairs
        with the subtitle fragment directly above it) -> chapter
      * a "درس N" label fragment pairs with the title fragment at the same y
        (within 12pt, same page); the title fragment is the lesson
      * plain content line: lesson under the open chapter (if one exists), else
        only a genuine TOC entry (filter-late) survives into the pre-list
    If the TOC has no chapter markers (flat list, common in primary books and
    in this Math book), a single implicit chapter titled 'محتویات' is
    synthesized from the entries."""
    n = len(lines)
    chapters, front, back, pre = [], [], [], []
    cur = None
    i = 0
    while i < n:
        pagenum, title, pidx, y = lines[i]
        raw = title.strip()
        nc = _nd(raw)
        # 1) front/back-matter keyword lines
        if any(nc.startswith(_nd(k)) for k in FRONT_KW):
            front.append({"title":clean_title(raw)}); i+=1; continue
        if any(nc.startswith(_nd(k)) for k in BACK_KW):
            back.append({"title":clean_title(raw)}); i+=1; continue
        # 2) map-legend / timeline junk
        if _is_timeline_junk(raw):
            i+=1; continue
        # 3) explicit chapter marker "فصل ..."
        if nc.startswith("فصل"):
            cur = {"chapter_number":chapter_number(raw),"title":clean_title(raw),"lessons":[]}
            chapters.append(cur); i+=1; continue
        # 4) ordinal-led chapter marker: "دوم" / "سوم کار با داده‌ها ۷۱"
        om = _ORD_RE.match(nc)
        if om:
            word = om.group(1)
            rest = clean_title(re.sub(rf"^{re.escape(word)}\s*", "", raw))
            t = rest if (rest and not re.fullmatch(r"[۰-۹٠-٩0-9]+", rest)) else clean_title(raw)
            cur = {"chapter_number":NUM_WORDS.get(word),"title":t,"lessons":[]}
            chapters.append(cur); i+=1; continue
        # 5) dangling chapter subtitle: "تابع ۳۹" directly above a bare ordinal "دوم"
        if i+1 < n:
            _pn, nxt, pidx2, y2 = lines[i+1]
            if (pidx2 == pidx and abs(y2-y) <= 12 and _ordinal_of(nxt) is not None
                    and _label_num(nxt) is None):
                cur = {"chapter_number":NUM_WORDS.get(_ordinal_of(nxt)),
                       "title":clean_title(raw),"lessons":[]}
                chapters.append(cur); i+=2; continue
        # 6) lesson-label fragment "درس ۱" paired with its title fragment
        if _label_num(raw) is not None:
            t, step = raw, 1
            for j in (i+1, i-1):
                if 0 <= j < n:
                    _pnj, tj, pidxj, yj = lines[j]
                    if (pidxj == pidx and abs(yj-y) <= 12 and _label_num(tj) is None
                            and _ordinal_of(tj) is None and not _is_timeline_junk(tj)
                            and not _is_noise(tj)):
                        t, step = tj, 2 if j > i else 1
                        break
            if cur is not None:
                cur["lessons"].append({"lesson_number":len(cur["lessons"])+1,
                                       "title":clean_title(t)})
            else:
                pre.append(t)
            i += step
            continue
        # 7) plain content line
        if i+1 < n:
            _pn3, nxtp, pidx3, y3 = lines[i+1]
            if pidx3 == pidx and abs(y3-y) <= 12 and _label_num(nxtp) is not None:
                i += 1                  # a "درس N" label below claims this title fragment
                continue
        if cur is not None:
            if not _is_noise(raw):
                cur["lessons"].append({"lesson_number":len(cur["lessons"])+1,
                                       "title":clean_title(raw)})
        elif _is_toc_entry(pagenum, raw) and not _is_noise(raw):
            pre.append(raw)             # before any chapter: only genuine entries survive
        i += 1
    if not chapters and pre:
        chapters.append({"chapter_number":0,"title":"محتویات","lessons":
            [{"lesson_number":k+1,"title":clean_title(t)} for k,t in enumerate(pre)]})
    else:
        front.extend({"title":clean_title(t)} for t in pre)   # pre-chapter entries -> front matter
    return chapters, front, back

def extract_toc_from_pdf(pdf_path, grade, branch, subject, book_code, out_path):
    """Extract the TOC of a digital (text-layer) PDF. Returns a status dict."""
    doc = fitz.open(pdf_path)
    res = {"book_code":book_code,"grade":grade,"branch":branch,"subject":subject,
           "pdf":os.path.basename(pdf_path),"extraction_method":"text"}
    if not has_text_layer(doc, 15):
        res["status"]="no_text_layer"; doc.close(); return res
    # locate the TOC page(s) within the first 15 pages. Match on the diacritic-
    # stripped text so decorative spellings like اَلْفِـهـرِس (with harakat/tatweel
    # between the letters) still hit the فهرس root.
    toc_start=None
    for i in range(min(15, doc.page_count)):
        text = _nd(_page_text(doc[i]))
        if any(k in text for k in TOC_KEYWORDS):
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
        pl_full=pl
        pl=cut_at_junk(pl)                    # Phase D: stop at map-legend/timeline junk
        if off==0:
            lines.extend(pl); toc_pages+=1    # the فهرst page itself: unconditional
            if pl!=pl_full:
                break                         # junk on the فهرst page -> close
            continue
        # Phase E: stop when the page opens the preface body (فهرst is over)
        if page_starts_preface(pl):
            break
        # Phase B: keep only genuine TOC entries (line-level filter) plus the
        # structural fragments classify() needs (bare ordinals, "درس N" labels)
        pv=[l for l in pl if _is_toc_entry(l[0], l[1]) or _keep_fragment(l[1])]
        # Phase C: stop at the first continuation page with zero TOC entries
        if not pv:
            break
        lines.extend(pv); toc_pages+=1
        if pl!=pl_full:
            break                             # junk figure under the entries -> close
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
