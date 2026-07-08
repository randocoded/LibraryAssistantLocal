# library assistant

ai-powered book recommendations, checked against your local library's catalog.

## what it does

- you give it a book you liked
- it asks an ai (gemini / openai / anthropic) for 5 similar recs
- grabs cover art from open library (best-effort)
- scrapes your library's bibliocommons catalog to see what's actually available
- if your book title's ambiguous, it asks you to pick which one you meant first

## running it

needs an api key for gemini, openai, or anthropic — pick one in the app and paste your key in. (no local/offline fallback, so a key is required.)

```bash
pip install -r requirements.txt
python3 server.py
```

then open `http://localhost:8000` in your browser.

## files

- `server.py` — the backend: static file server + api routes (`/api/clarify`, `/api/recommend`, `/api/test-key`)
- `static/index.html` — the page structure
- `static/app.js` — all the frontend logic (form handling, rendering results, etc)
- `static/styles.css` — the look, glassy dark theme

## notes

- catalog scraping is html-based (bibliocommons has no clean public api), so it's a bit fragile if they change their markup
- your api key gets saved in localStorage per-provider, so switching providers keeps each key around
- every api endpoint validates its inputs (book title length, allowed formats, allowed providers, domain must be `*.bibliocommons.com`, request size capped at 50kb) and always returns clean json errors instead of crashing — a broken/oversized/malicious request can't take the server down
- if the ai returns malformed json or garbage recommendation items, those get filtered out and you still get a sane fallback response instead of a 500
