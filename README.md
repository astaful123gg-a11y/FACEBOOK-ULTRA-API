# Facebook Download API (with key management)

Scrapes Facebook post/video/photo links using your logged-in session cookies.
Same caveat as the IG one: if Facebook changes its page structure or the
session expires, re-export `cookies.json` and redeploy.

## Setup
1. `pip install -r requirements.txt`
2. `cookies.json` — export from browser (list format with `name`/`value` per cookie, same as `cookies-2026-07-08.json`).
3. Admin key is hardcoded in `main.py` as `ADMIN_KEY = "asraful123"` — edit that line directly in code to change it (no env var needed).
4. Run: `python main.py`

## Deploy on Render
Push to a **private** repo (cookies.json has your session, and now the admin
key is hardcoded in main.py too — keep this repo private).
Build: `pip install -r requirements.txt`
Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Note: `keys.db` (SQLite) is created on first run. On Render's free tier the
filesystem is ephemeral — a redeploy wipes it, so all issued keys reset.
For a public API that should persist, either:
- upgrade to a Render disk (paid), or
- swap SQLite for a free hosted Postgres (e.g. Supabase/Neon).

## Admin endpoints (require header `X-Admin-Key: <ADMIN_KEY>`)

### Generate a new key
`POST /admin/generate`
```json
{"daily_limit": 50, "expires_in_days": 30, "note": "for @someuser"}
```
`expires_in_days: null` (or omit) = never expires.
Returns the generated key — give this to the user.

### Revoke a key (instantly blocks it)
`POST /admin/revoke`
```json
{"key": "shuvo_xxx..."}
```

### Un-revoke a key
`POST /admin/unrevoke`
```json
{"key": "shuvo_xxx..."}
```

### Update limit/expiry on an existing key
`POST /admin/update`
```json
{"key": "shuvo_xxx...", "daily_limit": 100, "expires_in_days": 0}
```
`expires_in_days: 0` = make it never expire. Omit a field to leave it unchanged.

### List all keys + today's usage
`GET /admin/keys`

### Delete a key permanently
`DELETE /admin/delete`
```json
{"key": "shuvo_xxx..."}
```

## Public endpoints (require header `X-API-Key: <issued key>`)

### Download a Facebook post/video/reel/album
`POST /api/download`
```json
{"url": "https://www.facebook.com/watch/?v=..."}
```
Response (video):
```json
{"type": "video", "caption": "...", "media": [{"type": "video", "url": "..."}]}
```
Response (single photo):
```json
{"type": "image", "caption": "...", "media": [{"type": "image", "url": "..."}]}
```
Response (multi-photo album — if the post has 10 photos, all 10 come back):
```json
{"type": "album", "caption": "...", "count": 10, "media": [{"type": "image", "url": "..."}, ...]}
```
Each photo in an album is fetched one at a time with a short delay between
requests (`ALBUM_FETCH_DELAY_SECONDS` in `main.py`, default 0.7s) so Facebook
doesn't get hammered. Capped at `MAX_ALBUM_PHOTOS` (default 50) per post.

### Keyword photo search (searches Facebook itself, using your cookies.json session)
`GET /search?q=naruto&limit=10`
- `limit`: 1–100, default 10.
```json
{"query": "naruto", "type": "photo", "count": 10, "results": [{"url": "...", "title": "...", "permalink": "https://mbasic.facebook.com/photo.php?fbid=..."}]}
```

### Keyword video search (searches Facebook itself, using your cookies.json session)
`GET /vsearch?q=naruto&limit=5`
- `limit`: 1–20, default 5.
```json
{"query": "naruto", "type": "video", "count": 5, "results": [{"url": "...", "title": "...", "permalink": "https://mbasic.facebook.com/video.php?v=..."}]}
```
Both hit `mbasic.facebook.com/search/photos` and `.../search/videos` with your
logged-in session, collect unique result permalinks (paginating through "see
more" pages if needed), then open each permalink one at a time to grab the
real photo/video URL. Delays between requests
(`SEARCH_ITEM_DELAY_SECONDS` / `SEARCH_PAGE_DELAY_SECONDS` in `main.py`) keep
this gentle on Facebook instead of firing everything at once.
Since this depends on Facebook's own search page markup, if results come back
empty/thin, it likely means Facebook changed that markup — same caveat as the
main download endpoint.

Each key has its own daily counter — resets automatically at UTC midnight.
Exceeding it returns `429`.
