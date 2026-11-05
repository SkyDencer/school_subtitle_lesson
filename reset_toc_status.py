"""One-shot: reset all toc_status markers for a clean p2/p3 re-run."""
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
st = json.load(open(os.path.join(ROOT, "state.json"), encoding="utf-8"))
n = 0
for node, b in st["books"].items():
    if b.get("toc_status"):
        b["toc_status"] = None
        n += 1
tmp = os.path.join(ROOT, "state.json.tmp")
json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
os.replace(tmp, os.path.join(ROOT, "state.json"))
books = json.load(open(os.path.join(ROOT, "book_inventory.json"), encoding="utf-8"))
for b in books:
    b["toc_status"] = None
json.dump(books, open(os.path.join(ROOT, "book_inventory.json"), "w",
          encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"reset {n} state entries + {len(books)} inventory entries -> toc_status=None")
