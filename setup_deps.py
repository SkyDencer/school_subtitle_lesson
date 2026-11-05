"""Fetch the wheels we need via stdlib urllib (pip's TLS path is flaky here),
then install them offline with pip --no-index. Kept separate from any
untrusted content; runs against pypi.org only."""
import json
import sys
import urllib.request
import urllib.error
import os

# packages + their transitive deps (pure-python or win_amd64 wheels available)
PACKAGES = [
    "pymupdf",
    "requests",
    "beautifulsoup4",
    "urllib3",
    "certifi",
    "charset-normalizer",
    "idna",
    "soupsieve",
]

WHEELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wheels")
os.makedirs(WHEELS_DIR, exist_ok=True)

# prefer win_amd64 C wheels, then abi3, then any pure-python wheel
def score(filename):
    s = 0
    low = filename.lower()
    if "win_amd64" in low:
        s += 100
    elif "abi3" in low and "whl" in low:
        s += 50
    if "py3-none-any" in low or "py2.py3-none-any" in low:
        s += 40
    if "win" in low:
        s += 20
    if low.endswith(".whl"):
        s += 10
    else:
        s -= 1000  # skip sdist
    return s

def latest_version_files(pkg):
    url = f"https://pypi.org/pypi/{pkg}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "setup-deps/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    ver = data["info"]["version"]
    files = data["releases"].get(ver, [])
    return ver, files

for pkg in PACKAGES:
    try:
        ver, files = latest_version_files(pkg)
    except urllib.error.HTTPError as e:
        print(f"!! {pkg}: HTTP {e.code}"); continue
    cands = [f for f in files if f["filename"].endswith(".whl")]
    cands.sort(key=lambda f: score(f["filename"]), reverse=True)
    if not cands:
        print(f"!! {pkg}: no wheel candidates (versions w/ wheels may differ)"); continue
    pick = cands[0]
    dst = os.path.join(WHEELS_DIR, pick["filename"])
    print(f"-> {pkg} {ver} :: {pick['filename']}")
    try:
        with urllib.request.urlopen(pick["url"], timeout=120) as resp:
            with open(dst, "wb") as out:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
        print(f"   saved {dst} ({os.path.getsize(dst)} bytes)")
    except Exception as e:
        print(f"!! {pkg}: download failed: {e}")

print("\nAll downloads attempted. Next step: pip install --no-index --find-links=./wheels ...")
