"""Fetch just the two wheels that failed / were wrong ABI, with resume + verify.
Uses stdlib urllib (TLS works here) and files.pythonhosted.org Range support."""
import os, json, zipfile, time, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
WHEELS = os.path.join(ROOT, "wheels")

# exact preferred wheel filenames
WANT = {
    "pymupdf": "pymupdf-2.1-cp311-abi3-win_amd64.whl",
    "charset-normalizer": "charset_normalizer-3.5.2-cp37-abi3-win_amd64.whl",
}

def url_for(pkg, fname):
    data = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30))
    ver = data["info"]["version"]
    for f in data["releases"].get(ver, []):
        if f["filename"] == fname:
            return f["url"], f.get("size")
    raise SystemExit(f"wheel not found in latest release: {fname}")

def download_with_resume(url, dst, size_hint, retries=8, chunk=1<<15):
    for attempt in range(1, retries+1):
        existing = os.path.getsize(dst) if os.path.exists(dst) else 0
        req = urllib.request.Request(url, headers={"User-Agent": "fetch-wheels/1.0"})
        if existing:
            req.add_header("Range", f"bytes={existing}-")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                # 200 = server ignored Range (start over); 206 = resume ok
                mode = "ab" if r.status == 206 else "wb"
                if mode == "wb":
                    existing = 0
                with open(dst, mode) as out:
                    while True:
                        c = r.read(chunk)
                        if not c:
                            break
                        out.write(c)
                        existing += len(c)
        except Exception as e:
            print(f"   attempt {attempt}: interrupted ({e}); resume at {existing} bytes")
            time.sleep(1.5)
            continue
        # verify zip
        try:
            with zipfile.ZipFile(dst) as z:
                assert z.testzip() is None
            if size_hint and os.path.getsize(dst) != size_hint:
                print(f"   size mismatch (got {os.path.getsize(dst)} want {size_hint}); re-downloading")
                os.remove(dst); continue
            return True
        except Exception as e:
            print(f"   attempt {attempt}: bad/corrupt ({e}); will resume/restart")
            continue
    return False

for pkg, fname in WANT.items():
    url, size = url_for(pkg, fname)
    dst = os.path.join(WHEELS, fname)
    # remove any stale/wrong-abi artifact for charset (cp310 copy we downloaded earlier)
    if pkg == "charset-normalizer":
        for stale in os.listdir(WHEELS):
            if stale.startswith("charset_normalizer-") and stale != fname:
                os.remove(os.path.join(WHEELS, stale)); print(f"   removed stale {stale}")
    print(f"fetching {fname} ({size} bytes)")
    ok = download_with_resume(url, dst, size)
    print(f"   {'SUCCESS' if ok else 'FAILED'} -> {dst}")
