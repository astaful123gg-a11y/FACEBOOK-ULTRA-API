import os
import re
import json
import time
import sqlite3
import secrets
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

import requests
from fastapi import FastAPI, HTTPException, Header, Depends, Query
from pydantic import BaseModel

app = FastAPI(title="Facebook Download API")

# ---------------------------------------------------------------- config

ADMIN_KEY = "asraful123"   # hardcoded on purpose — change this string directly in code if you rotate it
DB_PATH = os.path.join(os.path.dirname(__file__), "keys.db")

# ---- /search and /vsearch limits ----
MAX_PHOTO_RESULTS = 100
MAX_VIDEO_RESULTS = 20
DEFAULT_PHOTO_RESULTS = 10
DEFAULT_VIDEO_RESULTS = 5

# ---- album/multi-photo download tuning ----
MAX_ALBUM_PHOTOS = 50
ALBUM_FETCH_DELAY_SECONDS = 0.7   # delay between per-photo requests so we don't hammer Facebook

# ---- /search and /vsearch tuning (searches Facebook itself, using your logged-in cookies) ----
SEARCH_ITEM_DELAY_SECONDS = 0.6   # delay between opening each individual photo/video permalink
SEARCH_PAGE_DELAY_SECONDS = 1.2   # delay between "see more results" pages when paginating
MAX_SEARCH_PAGES = 10             # safety cap so a big `limit` can't loop forever

_cookies_file = os.path.join(os.path.dirname(__file__), "cookies.json")
with open(_cookies_file) as f:
    _raw_cookies = json.load(f)

# cookies.json here is a browser-export list of {name, value, ...} objects -> convert to a plain dict
if isinstance(_raw_cookies, list):
    COOKIES = {c["name"]: c["value"] for c in _raw_cookies if "name" in c and "value" in c}
else:
    COOKIES = _raw_cookies

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.cookies.update(COOKIES)
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------- db setup

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key TEXT PRIMARY KEY,
            daily_limit INTEGER NOT NULL,
            expires_at TEXT,           -- NULL = never expires
            revoked INTEGER DEFAULT 0,
            note TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            key TEXT,
            date TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (key, date)
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------- auth

def check_admin(x_admin_key: str = Header(default=None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")


def check_auth(x_api_key: str = Header(default=None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key")

    conn = db()
    row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (x_api_key,)).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid API key")

    if row["revoked"]:
        conn.close()
        raise HTTPException(status_code=403, detail="This API key has been revoked")

    if row["expires_at"]:
        expires = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires:
            conn.close()
            raise HTTPException(status_code=403, detail="This API key has expired")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage_row = conn.execute(
        "SELECT count FROM usage WHERE key = ? AND date = ?", (x_api_key, today)
    ).fetchone()
    used = usage_row["count"] if usage_row else 0

    if used >= row["daily_limit"]:
        conn.close()
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached ({row['daily_limit']}/day). Try again tomorrow.",
        )

    conn.execute(
        """INSERT INTO usage (key, date, count) VALUES (?, ?, 1)
           ON CONFLICT(key, date) DO UPDATE SET count = count + 1""",
        (x_api_key, today),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- models

class UrlRequest(BaseModel):
    url: str


class GenerateKeyRequest(BaseModel):
    daily_limit: int = 50
    expires_in_days: int | None = None   # None = never expires
    note: str = ""


class KeyTarget(BaseModel):
    key: str


class UpdateKeyRequest(BaseModel):
    key: str
    daily_limit: int | None = None
    expires_in_days: int | None = None   # None = leave unchanged, 0 = never expires


# ---------------------------------------------------------------- admin: key management

@app.post("/admin/generate", dependencies=[Depends(check_admin)])
def generate_key(req: GenerateKeyRequest):
    new_key = "shuvo_" + secrets.token_urlsafe(24)
    expires_at = None
    if req.expires_in_days is not None:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=req.expires_in_days)).isoformat()

    conn = db()
    conn.execute(
        "INSERT INTO api_keys (key, daily_limit, expires_at, revoked, note, created_at) VALUES (?, ?, ?, 0, ?, ?)",
        (new_key, req.daily_limit, expires_at, req.note, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    return {
        "key": new_key,
        "daily_limit": req.daily_limit,
        "expires_at": expires_at,
        "note": req.note,
    }


@app.post("/admin/revoke", dependencies=[Depends(check_admin)])
def revoke_key(req: KeyTarget):
    conn = db()
    row = conn.execute("SELECT key FROM api_keys WHERE key = ?", (req.key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Key not found")
    conn.execute("UPDATE api_keys SET revoked = 1 WHERE key = ?", (req.key,))
    conn.commit()
    conn.close()
    return {"key": req.key, "revoked": True}


@app.post("/admin/unrevoke", dependencies=[Depends(check_admin)])
def unrevoke_key(req: KeyTarget):
    conn = db()
    row = conn.execute("SELECT key FROM api_keys WHERE key = ?", (req.key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Key not found")
    conn.execute("UPDATE api_keys SET revoked = 0 WHERE key = ?", (req.key,))
    conn.commit()
    conn.close()
    return {"key": req.key, "revoked": False}


@app.post("/admin/update", dependencies=[Depends(check_admin)])
def update_key(req: UpdateKeyRequest):
    conn = db()
    row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (req.key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Key not found")

    new_limit = req.daily_limit if req.daily_limit is not None else row["daily_limit"]

    new_expires = row["expires_at"]
    if req.expires_in_days is not None:
        new_expires = None if req.expires_in_days == 0 else (
            datetime.now(timezone.utc) + timedelta(days=req.expires_in_days)
        ).isoformat()

    conn.execute(
        "UPDATE api_keys SET daily_limit = ?, expires_at = ? WHERE key = ?",
        (new_limit, new_expires, req.key),
    )
    conn.commit()
    conn.close()
    return {"key": req.key, "daily_limit": new_limit, "expires_at": new_expires}


@app.get("/admin/keys", dependencies=[Depends(check_admin)])
def list_keys():
    conn = db()
    rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = []
    for r in rows:
        usage_row = conn.execute(
            "SELECT count FROM usage WHERE key = ? AND date = ?", (r["key"], today)
        ).fetchone()
        result.append({
            "key": r["key"],
            "daily_limit": r["daily_limit"],
            "used_today": usage_row["count"] if usage_row else 0,
            "expires_at": r["expires_at"],
            "revoked": bool(r["revoked"]),
            "note": r["note"],
            "created_at": r["created_at"],
        })
    conn.close()
    return {"count": len(result), "keys": result}


@app.delete("/admin/delete", dependencies=[Depends(check_admin)])
def delete_key(req: KeyTarget):
    conn = db()
    conn.execute("DELETE FROM api_keys WHERE key = ?", (req.key,))
    conn.execute("DELETE FROM usage WHERE key = ?", (req.key,))
    conn.commit()
    conn.close()
    return {"key": req.key, "deleted": True}


# ---------------------------------------------------------------- health

@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Facebook Download API",
        "auth": "Header X-API-Key required on /api/* routes (generate one via /admin/generate)",
        "endpoints": {
            "download": {"method": "POST", "path": "/api/download", "body": {"url": "facebook post/video/reel link"}},
            "search": {"method": "GET", "path": "/search", "query": {"q": "keyword", "limit": f"1-{MAX_PHOTO_RESULTS} (default {DEFAULT_PHOTO_RESULTS})"}},
            "vsearch": {"method": "GET", "path": "/vsearch", "query": {"q": "keyword", "limit": f"1-{MAX_VIDEO_RESULTS} (default {DEFAULT_VIDEO_RESULTS})"}},
        },
    }


# ---------------------------------------------------------------- facebook scraping helpers

def _to_mbasic(url: str) -> str:
    return re.sub(r"https?://(www\.|web\.|m\.)?facebook\.com", "https://mbasic.facebook.com", url)


def _extract_video_urls(html: str):
    urls = {}
    for key in ["playable_url_quality_hd", "playable_url"]:
        m = re.search(rf'"{key}":"([^"]+)"', html)
        if m:
            urls[key] = m.group(1).encode().decode("unicode_escape").replace("\\/", "/")
    return urls


def _extract_image_url(html: str):
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m:
        return m.group(1).replace("&amp;", "&")
    return None


def _extract_title(html: str):
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if m:
        return m.group(1).replace("&amp;", "&")
    return None


def _extract_photo_permalinks(html: str, max_links: int, seen_fbids: set = None):
    """Find individual photo permalinks (/photo.php?fbid=...) in an HTML page —
    used both for multi-photo albums and for /search result pages.
    Returns a de-duplicated list of absolute mbasic photo.php URLs, in order of
    first appearance, capped at max_links."""
    if seen_fbids is None:
        seen_fbids = set()
    hrefs = re.findall(r'href="(/photo\.php\?fbid=\d+[^"]*)"', html)
    links = []
    for href in hrefs:
        m = re.search(r"fbid=(\d+)", href)
        if not m:
            continue
        fbid = m.group(1)
        if fbid in seen_fbids:
            continue
        seen_fbids.add(fbid)
        clean_href = href.replace("&amp;", "&")
        links.append("https://mbasic.facebook.com" + clean_href)
        if len(links) >= max_links:
            break
    return links


def _extract_video_permalinks(html: str, max_links: int, seen_vids: set = None):
    """Find individual video permalinks (/video.php?v=... or /watch/?v=...) in an
    HTML page — used for /vsearch result pages. Same de-dup + cap approach as
    _extract_photo_permalinks."""
    if seen_vids is None:
        seen_vids = set()
    hrefs = re.findall(r'href="(/(?:video\.php\?v=\d+|watch/\?v=\d+)[^"]*)"', html)
    links = []
    for href in hrefs:
        m = re.search(r"v=(\d+)", href)
        if not m:
            continue
        vid = m.group(1)
        if vid in seen_vids:
            continue
        seen_vids.add(vid)
        clean_href = href.replace("&amp;", "&")
        links.append("https://mbasic.facebook.com" + clean_href)
        if len(links) >= max_links:
            break
    return links


def _find_next_search_page(html: str, kind: str, visited_urls: set):
    """Best-effort 'See More Results' pagination link finder for mbasic search
    pages. Facebook's markup here isn't guaranteed stable, so this looks for any
    /search/{kind}/ link on the page with a cursor-like param that we haven't
    already visited, and takes the last one (the 'more' link is normally at the
    bottom of the page)."""
    candidates = re.findall(rf'href="(/search/{kind}/\?[^"]+)"', html)
    for href in reversed(candidates):
        clean_href = href.replace("&amp;", "&")
        full_url = "https://mbasic.facebook.com" + clean_href
        if full_url not in visited_urls and ("cursor=" in clean_href or "nm=" in clean_href):
            return full_url
    return None


# ---------------------------------------------------------------- /api/download

@app.post("/api/download", dependencies=[Depends(check_auth)])
def download(req: UrlRequest):
    try:
        r = SESSION.get(req.url, timeout=20, allow_redirects=True)
        html = r.text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Facebook fetch failed: {e}")

    video_urls = _extract_video_urls(html)
    caption = _extract_title(html)

    if video_urls:
        best_url = video_urls.get("playable_url_quality_hd") or video_urls.get("playable_url")
        return {
            "type": "video",
            "caption": caption,
            "media": [{"type": "video", "url": best_url}],
        }

    # multi-photo album/post: 10 photos in the post -> all 10 come back, 1 photo -> just 1
    album_links = _extract_photo_permalinks(html, MAX_ALBUM_PHOTOS)
    if len(album_links) > 1:
        media = []
        for i, link in enumerate(album_links):
            try:
                pr = SESSION.get(link, timeout=20, allow_redirects=True)
                photo_url = _extract_image_url(pr.text)
            except Exception:
                photo_url = None
            if photo_url:
                media.append({"type": "image", "url": photo_url})
            if i < len(album_links) - 1:
                time.sleep(ALBUM_FETCH_DELAY_SECONDS)  # be gentle on Facebook between requests

        if media:
            return {
                "type": "album",
                "caption": caption,
                "count": len(media),
                "media": media,
            }

    image_url = _extract_image_url(html)
    if image_url:
        return {
            "type": "image",
            "caption": caption,
            "media": [{"type": "image", "url": image_url}],
        }

    raise HTTPException(
        status_code=400,
        detail="Could not find downloadable media. Post may be private, deleted, or Facebook changed its page structure.",
    )


# ---------------------------------------------------------------- /search and /vsearch (searches Facebook itself, via your cookies)

def _search_facebook_photos(query: str, limit: int):
    limit = max(1, min(limit, MAX_PHOTO_RESULTS))

    seen_fbids = set()
    permalinks = []
    visited_pages = set()
    url = f"https://mbasic.facebook.com/search/photos/?q={quote(query)}"
    pages_fetched = 0

    while url and len(permalinks) < limit and pages_fetched < MAX_SEARCH_PAGES:
        try:
            r = SESSION.get(url, timeout=20, allow_redirects=True)
            html = r.text
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Facebook search failed: {e}")

        visited_pages.add(url)
        pages_fetched += 1
        remaining = limit - len(permalinks)
        permalinks.extend(_extract_photo_permalinks(html, remaining, seen_fbids))

        if len(permalinks) >= limit:
            break

        next_url = _find_next_search_page(html, "photos", visited_pages)
        url = next_url
        if url:
            time.sleep(SEARCH_PAGE_DELAY_SECONDS)  # be gentle between search result pages

    results = []
    for i, link in enumerate(permalinks):
        try:
            pr = SESSION.get(link, timeout=20, allow_redirects=True)
            photo_url = _extract_image_url(pr.text)
            title = _extract_title(pr.text)
        except Exception:
            photo_url, title = None, None
        if photo_url:
            results.append({"url": photo_url, "title": title, "permalink": link})
        if i < len(permalinks) - 1:
            time.sleep(SEARCH_ITEM_DELAY_SECONDS)  # be gentle between individual photo fetches

    return results


def _search_facebook_videos(query: str, limit: int):
    limit = max(1, min(limit, MAX_VIDEO_RESULTS))

    seen_vids = set()
    permalinks = []
    visited_pages = set()
    url = f"https://mbasic.facebook.com/search/videos/?q={quote(query)}"
    pages_fetched = 0

    while url and len(permalinks) < limit and pages_fetched < MAX_SEARCH_PAGES:
        try:
            r = SESSION.get(url, timeout=20, allow_redirects=True)
            html = r.text
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Facebook search failed: {e}")

        visited_pages.add(url)
        pages_fetched += 1
        remaining = limit - len(permalinks)
        permalinks.extend(_extract_video_permalinks(html, remaining, seen_vids))

        if len(permalinks) >= limit:
            break

        next_url = _find_next_search_page(html, "videos", visited_pages)
        url = next_url
        if url:
            time.sleep(SEARCH_PAGE_DELAY_SECONDS)  # be gentle between search result pages

    results = []
    for i, link in enumerate(permalinks):
        try:
            pr = SESSION.get(link, timeout=20, allow_redirects=True)
            video_urls = _extract_video_urls(pr.text)
            title = _extract_title(pr.text)
        except Exception:
            video_urls, title = {}, None
        best_url = video_urls.get("playable_url_quality_hd") or video_urls.get("playable_url")
        if best_url:
            results.append({"url": best_url, "title": title, "permalink": link})
        if i < len(permalinks) - 1:
            time.sleep(SEARCH_ITEM_DELAY_SECONDS)  # be gentle between individual video fetches

    return results


@app.get("/search", dependencies=[Depends(check_auth)])
def search_photos(
    q: str = Query(..., description="Search keyword, e.g. 'naruto'"),
    limit: int = Query(DEFAULT_PHOTO_RESULTS, description=f"Number of unique photos to pick, 1-{MAX_PHOTO_RESULTS}"),
):
    if limit < 1 or limit > MAX_PHOTO_RESULTS:
        raise HTTPException(status_code=400, detail=f"limit must be between 1 and {MAX_PHOTO_RESULTS}")

    photos = _search_facebook_photos(q, limit)
    return {
        "query": q,
        "type": "photo",
        "count": len(photos),
        "results": photos,
    }


@app.get("/vsearch", dependencies=[Depends(check_auth)])
def search_videos(
    q: str = Query(..., description="Search keyword, e.g. 'naruto'"),
    limit: int = Query(DEFAULT_VIDEO_RESULTS, description=f"Number of unique videos to pick, 1-{MAX_VIDEO_RESULTS}"),
):
    if limit < 1 or limit > MAX_VIDEO_RESULTS:
        raise HTTPException(status_code=400, detail=f"limit must be between 1 and {MAX_VIDEO_RESULTS}")

    videos = _search_facebook_videos(q, limit)
    return {
        "query": q,
        "type": "video",
        "count": len(videos),
        "results": videos,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
