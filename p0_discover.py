#!/usr/bin/env python
"""
Phase 0 — Book discovery & inventory for chap.sch.ir (academic track, grades 1-12).

Strategy (verified against the live site):
  * The site's advanced_search GET form filters books by category (tid) + year.
    Each academic grade/branch is a leaf tid. Querying it lists that leaf's
    books as /books/{node} links (one page fits; pagination handled if needed).
  * Each /books/{node} page exposes: title (subject_name), book code, and the
    current-year PDF ( /sites/default/files/lbooks/... ) — exactly what the
    inventory needs.

Outputs:
  * book_inventory.json  -> list of {book_code, grade, branch, subject_name, node, download_url, has_pdf}
  * state.json           -> resumable per-book / per-leaf progress
  * pipeline.log         -> every action

Idempotent & resumable: re-running skips leaves/books already recorded in
state.json. All network calls use requests with retries + exponential backoff.
"""

import os, re, json, time, datetime, sys
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state.json")
INV   = os.path.join(ROOT, "book_inventory.json")
LOG   = os.path.join(ROOT, "pipeline.log")
BASE  = "http://chap.sch.ir"
YEAR  = "1402"          # 1405-1406 (current academic year)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"}
NAV = {"13077","13078","9000","6915","15088","15089","15090","15091","15092"}  # site-nav book links

# Academic-track leaves: (grade, branch_slug, tid, label)
LEAVES = [
    (1,"primary","8","پایه اول"),(2,"primary","9","پایه دوم"),
    (3,"primary","10","پایه سوم"),(4,"primary","12","پایه چهارم"),
    (5,"primary","13","پایه پنجم"),(6,"primary","32","پایه ششم"),
    (7,"secondary","555","پایه هفتم"),(8,"secondary","558","پایه هشتم"),
    (9,"secondary","556","پایه نهم"),
    (10,"riazi","29","گ10 ریاضی-فیزیک"),(10,"tajrobi","40","گ10 علوم تجربی"),
    (10,"ensani","37","گ10 علوم انسانی"),(10,"maarefi","43","گ10 علوم و معارف اسلامی"),
    (11,"riazi","30","گ11 ریاضی-فیزیک"),(11,"tajrobi","41","گ11 علوم تجربی"),
    (11,"ensani","38","گ11 علوم انسانی"),(11,"maarefi","44","گ11 علوم و معارف اسلامی"),
    (12,"riazi","711","گ12 ریاضی-فیزیک"),(12,"tajrobi","712","گ12 علوم تجربی"),
    (12,"ensani","709","گ12 علوم انسانی"),(12,"maarefi","710","گ12 علوم و معارف اسلامی"),
]

# ------------------------------------------------- logging + state
def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] [p0] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)

def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"leaves": {}, "books": {}}

def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)

# ------------------------------------------------- network (retries + backoff)
sess = requests.Session()
sess.headers.update(HEADERS)
def get(url, tries=4):
    last = None
    for i in range(tries):
        try:
            r = sess.get(url, timeout=45)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(1.5 * (2 ** i))
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")

def soup(url):
    return BeautifulSoup(get(url), "html.parser")

# ------------------------------------------------- crawl a leaf -> book nodes
def leaf_nodes(tid):
    nodes = {}
    page = 1
    while True:
        params = {"tid": tid, "field_year_tid": YEAR, "title": ""}
        if page > 1:
            params["page"] = str(page)
        s = soup(f"{BASE}/advanced_search?" + "&".join(f"{k}={v}" for k, v in params.items()))
        found = False
        for a in s.find_all("a", href=True):
            m = re.search(r"/books/(\d+)", a["href"])
            if m and m.group(1) not in NAV:
                found = True
                nodes.setdefault(m.group(1), None)
        if page > 1 and not found:
            break
        has_next = re.search(r"/advanced_search\?[^\"']*\bpage=" + str(page + 1), s.get_text())
        if not has_next:
            break
        page += 1
        time.sleep(0.3)
    return list(nodes.keys())

# ------------------------------------------------- resolve one book page
def resolve_book(node):
    html = get(f"{BASE}/books/{node}")
    s = BeautifulSoup(html, "html.parser")
    title = (s.title.get_text() if s.title else "").split("|")[0].strip()
    txt = " ".join((s.find("main") or s.body).get_text(" ", strip=True).split())
    code = re.search(r"کد کتاب[:\s]*([0-9]+)", txt)
    code = code.group(1) if code else None
    # The lbooks PDF URL lives in the href/src ATTRIBUTE (not the visible text).
    pdfs = []
    for a in s.find_all("a", href=True):
        if "/lbooks/" in a["href"] and a["href"].lower().endswith(".pdf"):
            pdfs.append(a["href"] if a["href"].startswith("http") else BASE + a["href"])
    for img in s.find_all("img", src=True):
        if "/lbooks/" in img["src"]:
            pdfs.append(img["src"] if img["src"].startswith("http") else BASE + img["src"])
    url = None
    for p in pdfs:
        if "/1405-1406/" in p or (code and f"C{code}.pdf" in p):
            url = p
            break
    if url is None and pdfs:
        url = pdfs[0]
    return {"node": node, "title": title, "book_code": code, "download_url": url,
            "has_pdf": bool(url)}

YEAR_LABEL = "1405-1406"

# ============================================================================
def main():
    st = load_state()
    log(f"state: {len(st['books'])} books already resolved")
    for grade, branch, tid, label in LEAVES:
        key = str(tid)
        if st["leaves"].get(key, {}).get("done") and st["leaves"][key].get("nodes"):
            log(f"leaf {label} (tid {tid}): cached ({len(st['leaves'][key]['nodes'])} nodes) -> skip crawl")
        else:
            try:
                nodes = leaf_nodes(tid)
                st["leaves"][key] = {"grade": grade, "branch": branch, "label": label,
                                     "nodes": nodes, "done": True}
                save_state(st)
                log(f"leaf {label} (tid {tid}): crawled {len(nodes)} book nodes")
            except Exception as e:
                log(f"leaf {label} (tid {tid}): CRAWL ERROR {e}")
                st["leaves"][key] = {"grade": grade, "branch": branch, "label": label,
                                     "nodes": st["leaves"].get(key, {}).get("nodes", []),
                                     "done": False, "error": str(e)}
                save_state(st)
                continue
        for node in st["leaves"][key]["nodes"]:
            if st["books"].get(node, {}).get("url_status") == "resolved":
                continue
            try:
                info = resolve_book(node)
                info.update({"grade": grade, "branch": branch, "url_status": "resolved"})
                st["books"][node] = info
                save_state(st)
                log(f"book node {node} -> code={info['book_code']} title='{info['title']}' "
                    f"pdf={'yes' if info['has_pdf'] else 'NO'}")
                time.sleep(0.25)
            except Exception as e:
                st["books"][node] = {"node": node, "grade": grade, "branch": branch,
                                     "url_status": "failed", "error": str(e)}
                save_state(st)
                log(f"book node {node}: RESOLVE FAILED ({e})")

    # build inventory from resolved books
    inv = []
    for node, b in st["books"].items():
        if b.get("url_status") != "resolved":
            continue
        inv.append({"book_code": b.get("book_code"), "grade": b.get("grade"),
                    "branch": b.get("branch"), "subject_name": b.get("title"),
                    "node": node, "download_url": b.get("download_url"),
                    "has_pdf": b.get("has_pdf")})
    inv.sort(key=lambda x: (x["grade"], x["branch"], (x["book_code"] or "")))
    with open(INV, "w", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False, indent=1)
    log(f"inventory written -> {INV} ({len(inv)} books)")

    # per-grade report
    by_grade = {}
    for b in inv:
        by_grade.setdefault(b["grade"], 0)
        by_grade[b["grade"]] += 1
    print("\n=== PHASE 0 INVENTORY COUNTS (grade: books) ===")
    for g in range(1, 13):
        print(f"  grade {g:>2}: {by_grade.get(g,0)} books" + ("   [EMPTY!]" if by_grade.get(g,0)==0 else ""))
    no_pdf = [b for b in inv if not b.get("has_pdf")]
    print(f"\n  total inventory: {len(inv)} books | missing-PDF: {len(no_pdf)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
