"""Compute the transitive wheel closure for a set of top-level packages using the
PyPI JSON API (requires_dist), pick ABI-correct wheels for cp314/win_amd64,
download any that are missing (resume + zip-verify), so pip can install offline."""
import json, os, re, time, urllib.request, zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
WHEELS = os.path.join(ROOT, "wheels")
TOP = ["requests", "beautifulsoup4", "pymupdf"]

def has(fname):
    for f in os.listdir(WHEELS) if os.path.isdir(WHEELS) else []:
        if f == fname:
            p = os.path.join(WHEELS, f)
            try:
                with zipfile.ZipFile(p) as z:
                    return z.testzip() is None
            except Exception:
                return False
    return False

# --- marker filtering (minimal: honour extra==, python_version, sys_platform) ---
PY_VER = (3, 14)
PY_FULL = "3.14.2"
def cmp_ver(a, op, b):
    def norm(v):
        v = v.strip().strip("'\"")
        parts = re.findall(r"\d+", v)
        parts = [int(x) for x in (parts + [0, 0, 0])[:3]]
        return tuple(parts)
    A, B = norm(a), norm(b)
    return {"<": A < B, "<=": A <= B, ">": A > B, ">=": A >= B,
            "==": A == B, "!=": A != B}.get(op, True)

def keep_requirement(req):
    """req like 'numpy>=1.24' or "idna (>=2.5,<4) ; python_version >= '3'" """
    if ";" in req:
        base, marker = [x.strip() for x in req.split(";", 1)]
    else:
        base, marker = req, ""
    if not marker:
        return True
    # drop extras we do not install
    if re.search(r"\bextra\s*==", marker):
        return False
    # python_version / python_full_version comparisons (AND of all present)
    for m in re.finditer(r"(python_full_version|python_version)\s*(<=|>=|==|!=|<|>)\s*['\"]([\d.]+)['\"]", marker):
        var, op, val = m.groups()
        a = PY_FULL if var == "python_full_version" else f"{PY_VER[0]}.{PY_VER[1]}"
        if not cmp_ver(a, op, val):
            return False
    # sys_platform == 'win32' style: we ARE win
    for m in re.finditer(r"sys_platform\s*(==|!=)\s*['\"]([\w]+)['\"]", marker):
        op, val = m.groups()
        ok = (val == "win32") == (op == "==")
        if not ok:
            return False
    return True

def base_name(req):
    m = re.match(r"([A-Za-z0-9._-]+)", req.split(";")[0].strip())
    return m.group(1).lower().replace("_", "-")

# --- build closure ---
meta = {}          # pkg(lower) -> {version, requires_dist}
def fetch(pkg):
    if pkg in meta:
        return meta[pkg]
    data = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30))
    ver = data["info"]["version"]
    meta[pkg] = {"version": ver, "requires_dist": data["info"].get("requires_dist") or [],
                 "files": [f for f in data["releases"].get(ver, []) if f["filename"].endswith(".whl")],
                 "urlmap": {f["filename"]: f["url"] for f in data["releases"].get(ver, [])}}
    return meta[pkg]

closure = set()
stack = list(TOP)
while stack:
    p = stack.pop()
    key = p.lower().replace("_", "-")
    if key in closure:
        continue
    closure.add(key)
    m = fetch(key)
    for req in m["requires_dist"]:
        if keep_requirement(req):
            stack.append(base_name(req))
print("CLOSURE:", sorted(closure))

# --- pick wheel per package ---
def pick_wheel(key, m):
    files = m["files"]
    def score(fn):
        s = 0
        low = fn.lower()
        if "py3-none-any" in low or "py2.py3-none-any" in low:
            s += 30
        if "cp314" in low and "win_amd64" in low:
            s += 100
        elif "abi3" in low and "win_amd64" in low:
            s += 90
        elif "cp314" in low and "win_amd64" in low:
            s += 100
        return s
    if not files:
        return None
    best = sorted(files, key=lambda f: score(f["filename"]), reverse=True)[0]
    return best

to_download = []
for key in sorted(closure):
    m = fetch(key)
    c = pick_wheel(key, m)
    if c is None:
        print(f"!! {key}: NO wheel in {m['version']} (may need sdist)"); continue
    fn = c["filename"]
    if has(fn):
        print(f"have {fn}")
        continue
    to_download.append((key, fn, c["url"]))

print(f"\nneed to download {len(to_download)} wheel(s)")
for key, fn, url in to_download:
    dst = os.path.join(WHEELS, fn)
    existing = os.path.getsize(dst) if os.path.exists(dst) else 0
    for attempt in range(1, 9):
        req = urllib.request.Request(url, headers={"User-Agent": "resolve-deps/1.0"})
        if existing:
            req.add_header("Range", f"bytes={existing}-")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                mode = "ab" if r.status == 206 else "wb"
                if mode == "wb":
                    existing = 0
                with open(dst, mode) as out:
                    while True:
                        ch = r.read(1 << 15)
                        if not ch:
                            break
                        out.write(ch); existing += len(ch)
            with zipfile.ZipFile(dst) as z:
                assert z.testzip() is None
            print(f"OK   {fn} ({os.path.getsize(dst)} b)")
            break
        except Exception as e:
            print(f"retry {attempt} {fn}: {e} (at {existing})"); time.sleep(1.5)
    else:
        print(f"!! failed {fn}")
print("\nDone. Now: pip install --no-index --find-links=./wheels requests beautifulsoup4 pymupdf")
