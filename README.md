# Facebook Search + Download API

Uses `mbasic.facebook.com` (Facebook's lightweight HTML version — easiest to
scrape, this is what most "old phone" / low-data Facebook clients use) plus
your session cookies. No official API for this, purely scraping.

## ⚠️ Honest reliability note
Facebook is **the most aggressive of all 5 platforms** we've scraped so far
about detecting and blocking automated access — more than YouTube, more than
Instagram. This is a best-effort implementation:
- If a search/download call fails, the error message includes a debug snippet
  (HTTP status + raw HTML sample) so we can see exactly what Facebook sent back,
  same approach that worked for fixing Instagram/Pinterest.
- Send me that error text and I'll adjust the parsing — Facebook's HTML
  structure varies and changes without notice, so this will likely need a
  few rounds of fixing based on real responses, same as the other APIs did.

## Auth
All `/api/*` calls need header:
```
X-API-Key: SHUVO-apis
```

## Endpoints

### `/api/search` — photo search
```json
{"query": "naruto", "limit": 10}
```
Max limit: **100**. Paces requests with a small delay between each photo
resolution so Facebook doesn't get hit with a burst of requests.

### `/api/vsearch` — video search
```json
{"query": "naruto", "limit": 5}
```
Max limit: **20**.

### `/api/download` — any post link
```json
{"url": "https://www.facebook.com/.../posts/..."}
```
- If it's a **video post** → returns 1 video.
- If it's a **photo/album post** → returns **all** photos in it (1 photo in
  the post = 1 back, 10 photos = 10 back), same pattern as the Instagram API.

## Setup
1. `pip install -r requirements.txt`
2. `cookies.json` already included (session cookies: `c_user`, `xs`, etc).
3. Run: `python main.py`

## Deploy on Render
Push to a **private** repo (cookies.json has your session token).
Build: `pip install -r requirements.txt`
Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
