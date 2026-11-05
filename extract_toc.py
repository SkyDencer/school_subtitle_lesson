#!/usr/bin/env python
"""
extract_toc.py
==============
Extract the Table of Contents (فهرست) of the Iranian Grade-10 Humanities
Persian-1 textbook (book code 110201) from C110201.pdf using PyMuPDF, and
emit it as JSON.

Design notes
------------
* The book PDF (generated from InDesign) stores its Persian (RTL) text layer
  in *visual* order, so `get_text("text")` / `get_text("blocks")` come out
  bidi-scrambled. We therefore reconstruct each logical line from
  `get_text("words")`: group the words of each visual line PyMuPDF reports,
  sort them right-to-left (descending x0) and re-join. This restores the true
  Persian reading order for essentially every line.
* A TOC line is  [<page_number>] [<title>] . The page number is always the
  first token (it sits far-right, so it sorts first), so we split the
  leading integer off.
* Normalisation (per task): ي->ی, ك->ک, Arabic-Indic 0-9 -> Persian 0-9,
  ASCII digits inside Persian titles -> Persian, collapse Unicode spaces
  (incl. the U+200A hair-space the typesetter scattered between some words),
  rejoin ZWNJ-split words, tidy spaced parentheses, drop the space before a
  colon / exclamation mark.
* A small, fully-documented curation layer fixes the few lines the source
  PDF itself typeset with a bidi/ZWNJ defect. Every applied fix is logged.

JSON follows the requested structure exactly, plus two clearly-labelled
extensions so no TOC entry is lost:
  {grade, branch, subject, book_code,
   chapters:[{chapter_number, title, lessons:[{lesson_number, title}]}],
   front_matter:[{title}], back_matter:[{title}]}

All actions are appended to toc_extractor.log.
"""

import os, re, json, datetime, sys
import fitz  # PyMuPDF (the task explicitly names the `fitz` API)

ROOT = os.path.dirname(os.path.abspath(__file__))
PDF  = os.path.join(ROOT, "C110201.pdf")
OUT  = os.path.join(ROOT, "grade-10", "ensani", "farsi1.json")
LOG  = os.path.join(ROOT, "toc_extractor.log")

BOOK = {"grade": 10, "branch": "ensani", "subject": "farsi1", "book_code": "110201"}

# ---------------------------------------------------------------- logging
def log(msg, echo=True):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if echo:
        print(line)

# ------------------------------------------------------------- normalisation
AR_D, PERS_D = "٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹"
_ASCII_D = "0123456789"
_ZWJ = "\u200c"
# any whitespace + zero-width break chars, INCLUDING the U+200A hair-space the
# typesetter scattered between some words, but NOT ZWNJ (U+200C)/ZWJ (U+200D)
_WS = r"[ \t\u00a0\u2000-\u200b\u202f\u3000]+"

def normalize(s: str) -> str:
    s = s.replace("\u064a", "\u06cc")      # Yeh (Arabic)  -> ی
    s = s.replace("\u06aa", "\u06a9")      # Kaf (Arabic)  -> ک
    for a, p in zip(AR_D, PERS_D):         # Arabic-Indic -> Persian digits
        s = s.replace(a, p)
    for a, p in zip(_ASCII_D, PERS_D):     # ASCII digits -> Persian digits
        s = s.replace(a, p)
    s = re.sub(rf"\s*{_ZWJ}\s*", _ZWJ, s)   # rejoin ZWNJ-split words
    s = re.sub(r"\(\s+", "(", s)            # tidy spaced parentheses
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(_WS, " ", s)                 # collapse all whitespace (incl. U+200A)
    s = re.sub(r"\s+:", ":", s)             # no space before a colon
    s = s.replace(" !", "!").replace("؟", "؟")
    s = re.sub(_WS, " ", s).strip()
    return s

# --------------------------------------------------------- curation (source PDF defects)
# (a) whole-title overrides: if the title contains `match`, use `clean`.
WHOLE_OVERRIDE = [
    ("فارسی واژه",  "واژه‌های فارسی",            "p39: source reversed the two words of the free-vocabulary section"),
    ("بوی گل", "شعرخوانی: بوی گل و ریحان",   "p57: ZWNJ/hair-space defect made the poem title read 'ریحان‌ها'; restored clean title"),
    ("(ادبیات بومی)۱","درس چهارم: درس آزاد (ادبیات بومی ۱)", "p37: move the footnote marker ۱ inside the parentheses (cf. p120 '...۲')"),
]
# (b) in-place substring fixes (labels / canonical single-word terms).
IN_PLACE = [
    ("روان خوانی", "روان‌خوانی",  "canonical ZWNJ form of the section label"),
    ("شعر خوانی",  "شعرخوانی",    "canonical single-word form of the section label"),
    ("واژه نامه",  "واژه‌نامه",   "canonical single-word term (glossary)"),
    ("کتاب نامه",  "کتاب‌نامه",   "canonical single-word term (bibliography)"),
]

def curate(title: str):
    for match, clean, why in WHOLE_OVERRIDE:
        if match in title:
            if title != clean:
                log(f"[curate] '{title}' -> '{clean}'  ({why})")
            return clean
    for sub, repl, why in IN_PLACE:
        if sub in title:
            new = title.replace(sub, repl)
            log(f"[curate] '{title}' -> '{new}'  ({why})")
            title = new
    return title

# ------------------------------------------------------------- classification
FRONT_KW = ("پیشگفتار", "ستایش")
BACK_KW  = ("نیایش", "واژه‌نامه", "کتاب‌نامه", "پاسخنامه", "پیوست", "ضمیمه")

def letters_only(s: str):
    """Strip everything except Persian letters + digits so keyword matches are
    robust to spaces / ZWNJ / diacritics."""
    return "".join(ch for ch in s if 0x0600 <= ord(ch) <= 0x06FF or ch in "۰۱۲۳۴۵۶۷۸۹0123456789")

def looks_like_toc(lines):
    kw = re.compile(r"فصل|درس|کارگاه|گنج حکمت|روان|شعرخوانی|شعر خوانی|پیشگفتار|ستایش|نیایش|واژه|کتاب")
    scored = sum(1 for _pn, text, _p, _y in lines if kw.search(text))
    return len(lines) > 3 and scored / len(lines) >= 0.5

NUM_WORDS = {"یکم": 1, "دوم": 2, "سوم": 3, "چهارم": 4,
             "پنجم": 5, "ششم": 6, "هفتم": 7, "هشتم": 8}
def chapter_number(title: str):
    m = re.match(r"^فصل\s+([^:،\s]+)", title)
    tok = m.group(1) if m else ""
    if tok in NUM_WORDS:
        return NUM_WORDS[tok]
    digits = re.sub(r"[^۰-۹0-9]", "", tok)
    if digits:
        return int(digits.translate(str.maketrans(PERS_D + _ASCII_D, _ASCII_D + _ASCII_D)))
    return None

# ------------------------------------------------------------- reconstruction
def reconstruct_lines(page, skip_heading="فهرست"):
    """Return [(page_no, title, page_index, y)] in top->bottom order."""
    words = page.get_text("words")
    groups = {}
    for x0, y0, x1, y1, word, bl, ln, _wn in words:
        groups.setdefault((bl, ln), []).append((x0, y0, x1, word))
    out = []
    for (bl, ln), ws in groups.items():
        if not ws:
            continue
        ycenter = sum(w[1] for w in ws) / len(ws)
        raw = " ".join(w[3] for w in sorted(ws, key=lambda t: -t[0]))
        norm = normalize(raw)
        m = re.match(r"^(\d+)\s+(.*)$", norm)
        pagenum, title = (int(m.group(1)), m.group(2).strip()) if m else (None, norm)
        if title == skip_heading or (not title and pagenum is None):
            continue
        out.append((pagenum, title, int(page.number), round(ycenter, 1)))
    out.sort(key=lambda t: t[3])
    return out

# ============================================================================
def main():
    log("[extract] opening %s" % os.path.basename(PDF))
    doc = fitz.open(PDF)
    total = doc.page_count
    log(f"[extract] document has {total} pages")

    # 1. locate فهرست within first 10 pages
    toc_start = None
    for i in range(min(10, total)):
        if "فهرست" in doc[i].get_text("text"):
            toc_start = i
            break
    if toc_start is None:
        log("[extract] ERROR: keyword 'فهرست' not found in first 10 pages")
        return 1
    log(f"[extract] فهرست heading found on page {toc_start+1} (0-based {toc_start})")

    # 2. pull the TOC page(s): this page + following page(s) while TOC-like
    lines = []
    for offset in (0, 1, 2):
        idx = toc_start + offset
        if idx >= total:
            break
        pl = reconstruct_lines(doc[idx])
        if offset == 0:
            lines.extend(pl); continue
        if not looks_like_toc(pl):
            log(f"[extract] page {idx+1} is not a TOC page -> stop here")
            break
        log(f"[extract] including TOC continuation page {idx+1}")
        lines.extend(pl)

    seen = set(); uniq = []
    for item in lines:
        key = (item[0], item[1])
        if key in seen:
            continue
        seen.add(key); uniq.append(item)
    lines = uniq
    log(f"[extract] reconstructed {len(lines)} unique TOC lines starting page {toc_start+1}")

    # 3. classify
    chapters, front_matter, back_matter = [], [], []
    cur = None
    lk = {k: letters_only(k) for k in BACK_KW}
    fk = {k: letters_only(k) for k in FRONT_KW}
    for pagenum, title, pidx, y in lines:
        clean = curate(title)
        log(f"[entry] p{pagenum} :: {clean}")
        lo = letters_only(clean)
        if any(lo.startswith(v) for v in fk.values()):
            front_matter.append({"title": clean}); continue
        if any(lo.startswith(v) for v in lk.values()):
            back_matter.append({"title": clean}); continue
        if re.match(r"فصل", clean):
            cur = {"chapter_number": chapter_number(clean), "title": clean, "lessons": []}
            chapters.append(cur)
            log(f"[chapter] {cur['chapter_number']}: {clean}")
            continue
        if cur is None:
            front_matter.append({"title": clean}); continue
        cur["lessons"].append({"lesson_number": len(cur["lessons"]) + 1, "title": clean})

    result = {**BOOK, "chapters": chapters,
              "front_matter": front_matter, "back_matter": back_matter}

    # 4. sanity + write
    assert chapters, "no chapters found"
    n_lessons = sum(len(c["lessons"]) for c in chapters)
    log(f"[extract] totals: {len(chapters)} chapters, {n_lessons} lessons, "
        f"{len(front_matter)} front-matter, {len(back_matter)} back-matter")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"[extract] wrote JSON -> {OUT}")
    doc.close()

    # 5. print JSON
    print("\n" + "=" * 72)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=" * 72)

    # 6. verification summary
    print("\nSUMMARY")
    print(f"  chapters      : {len(chapters)}")
    for c in chapters:
        print(f"    ch {c['chapter_number']}: {c['title']}  ({len(c['lessons'])} entries)")
    print(f"  total lessons : {n_lessons}")
    print(f"  front-matter  : {[x['title'] for x in front_matter]}")
    print(f"  back-matter   : {[x['title'] for x in back_matter]}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
