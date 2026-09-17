import os
import time
from urllib.parse import urlsplit, urlunsplit
import requests

url = os.getenv("RENDER_PROBE_URL", "").strip()
if not url:
    raise SystemExit(0)

parts = urlsplit(url)
safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
print(f"[RENDER-PROBE] START {safe_url}", flush=True)

try:
    started = time.monotonic()
    headers = {
        "User-Agent": "Mozilla/5.0 (Render-Diagnostics)",
        "Range": "bytes=0-0",
        "Accept": "*/*",
    }
    with requests.get(url, stream=True, timeout=(15, 30), headers=headers, allow_redirects=True) as r:
        elapsed = time.monotonic() - started
        final = urlsplit(r.url)
        safe_final = urlunsplit((final.scheme, final.netloc, final.path, "", ""))
        print(f"[RENDER-PROBE] STATUS {r.status_code}", flush=True)
        print(f"[RENDER-PROBE] FINAL {safe_final}", flush=True)
        print(f"[RENDER-PROBE] CONTENT_TYPE {r.headers.get('Content-Type', '')}", flush=True)
        print(f"[RENDER-PROBE] CONTENT_LENGTH {r.headers.get('Content-Length', '')}", flush=True)
        print(f"[RENDER-PROBE] CONTENT_RANGE {r.headers.get('Content-Range', '')}", flush=True)
        print(f"[RENDER-PROBE] SERVER {r.headers.get('Server', '')}", flush=True)
        print(f"[RENDER-PROBE] ELAPSED {elapsed:.2f}s", flush=True)
        try:
            sample = next(r.iter_content(chunk_size=512), b"")
            if sample:
                print(f"[RENDER-PROBE] BODY_HEX {sample[:256].hex()}", flush=True)
            else:
                print("[RENDER-PROBE] BODY_HEX <empty>", flush=True)
        except Exception as e:
            print(f"[RENDER-PROBE] BODY_READ_ERROR {type(e).__name__}: {e}", flush=True)
except Exception as e:
    print(f"[RENDER-PROBE] ERROR {type(e).__name__}: {e}", flush=True)

print("[RENDER-PROBE] END", flush=True)
