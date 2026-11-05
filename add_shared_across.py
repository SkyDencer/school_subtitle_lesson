"""One-shot (idempotent): add "shared_across" metadata to shared-book outputs.

A book that the Phase-0 crawl found under SEVERAL branch leaves (state.json ->
leaves[tid].nodes) is shared across those branches even though its JSON file
lives in only one branch's directory (the first branch that listed it).

This writes:
  * "shared_across": [branches] into each affected output/.../{code} - {subject}.json
  * "shared_across": [branches] into the matching book_inventory.json entries

Re-running is safe: the field is just re-derived and overwritten with the same
value. Books without 2+ branch listings lose the field (no-op if absent).
"""
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from p2_extract import out_path

# high-school tids from p0_discover.LEAVES: tid -> (grade, branch_slug)
LEAF_META = {"29": ("10", "riazi"), "40": ("10", "tajrobi"), "37": ("10", "ensani"),
             "43": ("10", "maarefi"), "30": ("11", "riazi"), "41": ("11", "tajrobi"),
             "38": ("11", "ensani"), "44": ("11", "maarefi"), "711": ("12", "riazi"),
             "712": ("12", "tajrobi"), "709": ("12", "ensani"), "710": ("12", "maarefi")}
ORDER = ["riazi", "tajrobi", "ensani", "maarefi"]

st = json.load(open(os.path.join(ROOT, "state.json"), encoding="utf-8"))
node_branches = {}
for tid, lv in st.get("leaves", {}).items():
    meta = LEAF_META.get(str(tid))
    if not meta:
        continue
    for node in lv.get("nodes", []):
        node_branches.setdefault(str(node), set()).add(meta[1])
shared = {n: sorted(brs, key=ORDER.index)
          for n, brs in node_branches.items() if len(brs) > 1}

inv = json.load(open(os.path.join(ROOT, "book_inventory.json"), encoding="utf-8"))
n_inv = n_json = 0
for b in inv:
    brs = shared.get(str(b["node"]))
    if not brs:
        b.pop("shared_across", None)
        continue
    b["shared_across"] = brs
    n_inv += 1
    fp = out_path(b)
    if os.path.exists(fp):
        d = json.load(open(fp, encoding="utf-8"))
        d["shared_across"] = brs
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        n_json += 1
with open(os.path.join(ROOT, "book_inventory.json"), "w", encoding="utf-8") as f:
    json.dump(inv, f, ensure_ascii=False, indent=1)
print(f"shared books (2+ branch leaves): {len(shared)}")
print(f"inventory entries updated: {n_inv} | output JSONs updated: {n_json}")
