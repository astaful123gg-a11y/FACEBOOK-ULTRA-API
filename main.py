import os
import re
import time
import json
import requests
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Facebook Search + Download API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PASSWORD = "SHUVO-apis"
MAX_PHOTO_SEARCH = 100
MAX_VIDEO_SEARCH = 20
DELAY = 0.6   # seconds between per-item resolution requests, to go easy on Facebook

_cookies_file = os.path.join(os.path.dirname(__file__), "cookies.json")
with open(_cookies_file) as f:
    COOKIES = json.load(f)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.cookies.update(COOKIES)
SESSION.headers.update(HEADERS)

MBASIC = "https://mbasic.facebook.com"
WWW = "https://www.facebook.com"


def check_auth(x_api_key: str = Header(default=None)):
    if x_api_key != API_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class SearchRequest(BaseModel):
    query: str
    limit: int = 10


class UrlRequest(BaseModel):
    url: str


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Facebook Search + Download API",
        "auth": "Header X-API-Key: SHUVO-apis (required on all /api/* routes)",
        "endpoints": {
            "search (/search)": {"method": "POST", "path": "/api/search", "body": {"query": "string", "limit": f"up to {MAX_PHOTO_SEARCH}"}},
            "vsearch (/vsearch)": {"method": "POST", "path": "/api/vsearch", "body": {"query": "string", "limit": f"up to {MAX_VIDEO_SEARCH}"}},
            "download (/download)": {"method": "POST", "path": "/api/download", "body": {"url": "post/photo/video link"}, "note": "returns ALL media items in the post"},
        },
    }


# ---------------------------------------------------------------- helpers

def _get(url, params=None, base=MBASIC):
    full = url if url.startswith("http") else f"{base}{url}"
    try:
        r = SESSION.get(full, params=params, timeout=20)
        return r
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Facebook request failed: {e}")


def resolve_video_url(post_url):
    """Facebook embeds direct mp4 urls (escaped) in the page's raw HTML/JSON as
    browser_native_hd_url / browser_native_sd_url. Regex them out."""
    r = _get(post_url, base=WWW)
    html = r.text

    for key in ("browser_native_hd_url", "browser_native_sd_url", "playable_url_quality_hd", "playable_url"):
        m = re.search(rf'"{key}":"([^"]+)"', html)
        if m:
            return m.group(1).replace("\\/", "/").encode().decode("unicode_escape")

    return None


def resolve_photo_url(photo_permalink):
    """A single mbasic photo permalink page — grab the largest <img> on it."""
    r = _get(photo_permalink)
    html = r.text

    candidates = re.findall(r'<img[^>]+src="([^"]+)"', html)
    scontent = [u for u in candidates if "scontent" in u]
    if not scontent:
        return None
    # mbasic usually lists the main photo first among scontent images
    return scontent[0].replace("&amp;", "&")


def extract_post_id(url):
    m = re.search(r"(?:posts|photo\.php|photos|videos|watch|reel)[/?].*?(?:fbid=|/)([\w.\-]+)", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------- /download

@app.post("/api/download", dependencies=[Depends(check_auth)])
def download(req: UrlRequest):
    url = req.url.strip()
    is_video = bool(re.search(r"(/videos/|/watch/|/reel/|watch\?v=)", url))

    if is_video:
        video_url = resolve_video_url(url)
        if not video_url:
            raise HTTPException(status_code=400, detail="Could not resolve video (post may be private, or Facebook changed its page structure — try again or send the raw response for debugging)")
        return {"type": "video", "count": 1, "media": [{"type": "video", "url": video_url}]}

    # photo / album post
    r = _get(url, base=MBASIC)
    html = r.text

    photo_links = list(dict.fromkeys(re.findall(r'href="(/photo\.php\?fbid=[^"]+)"', html)))
    if not photo_links:
        # maybe it's already a single photo permalink itself
        single = resolve_photo_url(url if url.startswith("http") else f"{MBASIC}{url}")
        if not single:
            raise HTTPException(status_code=400, detail="No photos found in this post — it may be private, a video, or Facebook changed its page structure")
        return {"type": "photo", "count": 1, "media": [{"type": "image", "url": single}]}

    media = []
    for link in photo_links:
        full_url = resolve_photo_url(f"{MBASIC}{link.replace('&amp;', '&')}")
        if full_url:
            media.append({"type": "image", "url": full_url})
        time.sleep(DELAY)

    if not media:
        raise HTTPException(status_code=400, detail="Found photo links but couldn't resolve any full-size images")

    return {"type": "photo_album", "count": len(media), "media": media}


# ---------------------------------------------------------------- /search (photos)

@app.post("/api/search", dependencies=[Depends(check_auth)])
def search(req: SearchRequest):
    limit = max(1, min(req.limit, MAX_PHOTO_SEARCH))
    r = _get("/search/photos/", params={"q": req.query})
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Search failed: HTTP {r.status_code}")

    html = r.text
    photo_links = list(dict.fromkeys(re.findall(r'href="(/photo\.php\?fbid=[^"]+)"', html)))

    if not photo_links:
        raise HTTPException(
            status_code=400,
            detail=f"No results parsed. Debug: status={r.status_code}, html_len={len(html)}, "
                   f"sample={html[:200]!r}",
        )

    results = []
    for link in photo_links[:limit]:
        full_url = resolve_photo_url(f"{MBASIC}{link.replace('&amp;', '&')}")
        if full_url:
            results.append({
                "type": "image",
                "url": full_url,
                "post_url": f"{MBASIC}{link.replace('&amp;', '&')}",
            })
        time.sleep(DELAY)
        if len(results) >= limit:
            break

    return {"query": req.query, "count": len(results), "results": results}


# ---------------------------------------------------------------- /vsearch (videos)

@app.post("/api/vsearch", dependencies=[Depends(check_auth)])
def vsearch(req: SearchRequest):
    limit = max(1, min(req.limit, MAX_VIDEO_SEARCH))
    r = _get("/search/videos/", params={"q": req.query})
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Search failed: HTTP {r.status_code}")

    html = r.text
    video_links = list(dict.fromkeys(re.findall(r'href="(/[^"]*video[^"]*?/\?[^"]*)"', html, re.IGNORECASE)))

    if not video_links:
        raise HTTPException(
            status_code=400,
            detail=f"No results parsed. Debug: status={r.status_code}, html_len={len(html)}, "
                   f"sample={html[:200]!r}",
        )

    results = []
    for link in video_links[:limit]:
        full_link = f"{MBASIC}{link.replace('&amp;', '&')}"
        video_url = resolve_video_url(full_link)
        if video_url:
            results.append({"type": "video", "url": video_url, "post_url": full_link})
        time.sleep(DELAY)
        if len(results) >= limit:
            break

    return {"query": req.query, "count": len(results), "results": results}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
