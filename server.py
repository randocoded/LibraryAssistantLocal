import http.server
import json
import os
import urllib.parse
import re
import mimetypes
import requests
from bs4 import BeautifulSoup

PORT = 8000
MAX_BODY_BYTES = 50_000  # cap request size so someone can't send a 500mb blob and choke the server

VALID_PROVIDERS = {'gemini', 'openai', 'anthropic'}
VALID_FORMATS = {'Book', 'eBook', 'Downloadable Audiobook', 'DVD'}
DOMAIN_PATTERN = re.compile(r'^[a-z0-9-]+\.bibliocommons\.com$')  # only allow legit bibliocommons subdomains

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')  # static/ folder lives next to this file


def read_json_body(handler):
    """reads + parses the request body, w/ a size cap. returns (data, error_msg) — one will be None."""
    try:
        length = int(handler.headers.get('Content-Length', 0))
    except (TypeError, ValueError):
        return None, "Missing or invalid Content-Length header"

    if length <= 0:
        return None, "Empty request body"
    if length > MAX_BODY_BYTES:
        return None, "Request body too large"

    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode('utf-8'))
    except Exception:
        return None, "Invalid JSON payload"

    if not isinstance(data, dict):
        return None, "Request body must be a JSON object"

    return data, None


def clean_str(value, max_len=300):
    """coerce to a trimmed string, capped in length. non-strings become empty."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]


def validate_provider(provider):
    provider = clean_str(provider, 20).lower()
    if provider not in VALID_PROVIDERS:
        return None, f"api_provider must be one of: {', '.join(sorted(VALID_PROVIDERS))}"
    return provider, None


def validate_domain(domain):
    domain = clean_str(domain, 100).lower()
    if not domain or not DOMAIN_PATTERN.match(domain):
        return None, "library_domain must look like 'yourlib.bibliocommons.com'"
    return domain, None


def validate_formats(formats):
    if not isinstance(formats, list) or len(formats) == 0:
        return None, "formats must be a non-empty list"

    cleaned = [clean_str(f, 40) for f in formats]
    valid = [f for f in cleaned if f in VALID_FORMATS]

    if not valid:  # none of what they sent matched anything we recognize
        return None, f"formats must include at least one of: {', '.join(sorted(VALID_FORMATS))}"
    return valid, None


class LibraryAssistantHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # basic static file server: strip query params, default / -> /index.html
        path = self.path.split('?')[0]
        if path == '/':
            path = '/index.html'

        file_path = os.path.join(STATIC_DIR, path.lstrip('/'))  # map url path -> file under static/

        real_static_path = os.path.realpath(STATIC_DIR)  # security: prevent path traversal (../../etc/passwd vibes)
        real_file_path = os.path.realpath(file_path)

        # reject if: not inside static dir, doesn't exist, or is a dir (we only serve files)
        if (
            not real_file_path.startswith(real_static_path)
            or not os.path.exists(real_file_path)
            or os.path.isdir(real_file_path)
        ):
            self.send_error(404, "File Not Found")
            return

        try:
            with open(real_file_path, 'rb') as f:  # read + serve the file
                content = f.read()

            content_type, _ = mimetypes.guess_type(real_file_path)  # guess content-type so browser knows what it is
            if not content_type:
                content_type = 'application/octet-stream'

            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Internal Server Error: {str(e)}")  # if disk read fails etc

    def do_POST(self):
        try:  # outer safety net: no matter what goes wrong, we always send a real json response back
            if self.path == '/api/test-key':
                self._handle_test_key()
            elif self.path == '/api/clarify':
                self._handle_clarify()
            elif self.path == '/api/recommend':
                self._handle_recommend()
            else:
                self.send_error(404, "Endpoint Not Found")
        except Exception as e:
            import traceback
            traceback.print_exc()  # dump stack trace to server logs for debugging
            try:
                self.send_json_response(500, {"error": f"Unexpected server error: {str(e)}"})
            except Exception:
                pass  # connection's probably already dead at this point, nothing more we can do

    def _handle_test_key(self):
        # tiny endpoint to validate a provider key w/ minimal token usage
        params, err = read_json_body(self)
        if err:
            self.send_json_response(400, {"ok": False, "error": err})
            return

        provider, err = validate_provider(params.get('api_provider', 'gemini'))
        if err:
            self.send_json_response(400, {"ok": False, "error": err})
            return

        api_key = clean_str(params.get('api_key'), 500)
        if not api_key:  # no key = can't test it
            self.send_json_response(400, {"ok": False, "error": "No API key provided"})
            return

        result = self.test_provider_key(provider, api_key)
        self.send_json_response(200, result)

    def _handle_clarify(self):
        # endpoint: check if user input is ambiguous (multiple books match)
        params, err = read_json_body(self)
        if err:
            self.send_json_response(400, {"error": err})
            return

        book_input = clean_str(params.get('book_input'), 300)
        if not book_input:
            self.send_json_response(400, {"error": "book_input is required"})
            return

        provider, err = validate_provider(params.get('api_provider', 'gemini'))
        if err:
            self.send_json_response(400, {"error": err})
            return

        api_key = clean_str(params.get('api_key'), 500) or os.environ.get(f'{provider.upper()}_API_KEY')  # allow key from request OR env var like GEMINI_API_KEY / OPENAI_API_KEY / etc

        clarification = self.get_ai_clarification(book_input, api_key, provider)
        self.send_json_response(200, clarification)

    def _handle_recommend(self):
        # endpoint: main flow -> ai recs + covers + catalog scrape
        params, err = read_json_body(self)
        if err:
            self.send_json_response(400, {"error": err})
            return

        book_title = clean_str(params.get('book_title'), 300)
        if not book_title:
            self.send_json_response(400, {"error": "Book title is required"})
            return

        library_domain, err = validate_domain(params.get('library_domain', 'opl.bibliocommons.com'))
        if err:
            self.send_json_response(400, {"error": err})
            return

        preferred_formats, err = validate_formats(params.get('formats', ['Book', 'eBook', 'Downloadable Audiobook']))
        if err:
            self.send_json_response(400, {"error": err})
            return

        provider, err = validate_provider(params.get('api_provider', 'gemini'))
        if err:
            self.send_json_response(400, {"error": err})
            return

        api_key = clean_str(params.get('api_key'), 500) or os.environ.get(f'{provider.upper()}_API_KEY')

        try:
            recommendations = self.get_ai_recommendations(book_title, api_key, provider)  # 1) ask ai for 5 recs

            for rec in recommendations:  # 2) enrich each rec w/ cover + catalog matches
                rec['cover_url'] = self.get_open_library_cover(rec.get('title', ''), rec.get('author', ''))  # open library cover lookup (best-effort)

                rec['catalog_items'] = self.search_library_catalog(  # scrape biblio commons search results for availability
                    title=rec.get('title', ''),
                    author=rec.get('author', ''),
                    domain=library_domain,
                    preferred_formats=preferred_formats
                )

            self.send_json_response(200, {"recommendations": recommendations})

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_json_response(500, {"error": f"Error processing recommendations: {str(e)}"})

    def send_json_response(self, status, data):
        # small helper so we don't repeat headers everywhere
        response_bytes = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(response_bytes))
        self.end_headers()
        self.wfile.write(response_bytes)

    # ---------------------------------------------------------------
    # provider-agnostic ai helpers
    # ---------------------------------------------------------------

    @staticmethod
    def _strip_json_fences(content):
        # models sometimes wrap json in ```json ... ``` — this strips that so json.loads doesn't choke
        content = re.sub(r'^```json\s*', '', content.strip())
        content = re.sub(r'^```\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
        return content

    # --- gemini ---
    def _raw_call_gemini(self, key, prompt, json_mode=True):
        # raw call returns text (maybe json string)
        gemini_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-flash-latest:generateContent?key={key}"
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if json_mode:
            payload["generationConfig"] = {"responseMimeType": "application/json"}  # tells gemini to respond w/ json mime type (still comes back as text)

        r = requests.post(gemini_url, json=payload, timeout=20)
        if r.status_code == 200:
            return r.json()['candidates'][0]['content']['parts'][0]['text']
        raise Exception(f"Gemini returned {r.status_code}: {r.text}")

    def _call_gemini(self, key, prompt):
        return json.loads(self._strip_json_fences(self._raw_call_gemini(key, prompt, json_mode=True)))  # parse json response into python dict

    # --- openai ---
    def _raw_call_openai(self, key, prompt, json_mode=True):
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": prompt}]
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}  # forces valid json object output

        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code == 200:
            return r.json()['choices'][0]['message']['content']
        raise Exception(f"OpenAI returned {r.status_code}: {r.text}")

    def _call_openai(self, key, prompt):
        return json.loads(self._strip_json_fences(self._raw_call_openai(key, prompt, json_mode=True)))

    # --- anthropic ---
    def _raw_call_anthropic(self, key, prompt):
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}]
        }

        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code == 200:
            return r.json()['content'][0]['text']
        raise Exception(f"Anthropic returned {r.status_code}: {r.text}")

    def _call_anthropic(self, key, prompt):
        return json.loads(self._strip_json_fences(self._raw_call_anthropic(key, prompt)))

    def test_provider_key(self, provider, api_key):
        """lightweight, near-zero-token connectivity check for a single provider's key."""
        raw_callers = {
            'gemini': lambda: self._raw_call_gemini(api_key, "Reply with exactly one word: OK", json_mode=False),
            'openai': lambda: self._raw_call_openai(api_key, "Reply with exactly one word: OK", json_mode=False),
            'anthropic': lambda: self._raw_call_anthropic(api_key, "Reply with exactly one word: OK"),
        }

        call = raw_callers.get(provider)
        if not call:
            return {"ok": False, "error": f"Unknown provider '{provider}'"}

        try:
            text = call()
            return {"ok": True, "response": text.strip()[:50]}  # keep response tiny so we don't leak a whole essay into the ui
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _query_ai(self, prompt, api_key=None, provider='gemini'):
        """call the selected cloud provider — requires a valid api key."""
        providers = {
            'gemini': self._call_gemini,
            'openai': self._call_openai,
            'anthropic': self._call_anthropic,
        }

        if not api_key:
            raise Exception("No API key provided")

        call = providers.get(provider, self._call_gemini)
        return call(api_key, prompt)

    def get_ai_clarification(self, book_input, api_key=None, provider='gemini'):
        # asks ai: "is this title ambiguous?" and returns either needs_clarification + candidates[], or resolved_title/author
        prompt = (
            f"A user typed the following book title or description: '{book_input}'. "
            f"Determine whether this is ambiguous (i.e., there are multiple notable books with the same or very similar title, "
            f"or the title is very vague/unclear). "
            f"If it IS ambiguous, return a JSON object with: "
            f"  'needs_clarification': true, "
            f"  'candidates': a list of up to 5 distinct books (each with 'title', 'author', 'year' as integer, 'description': one sentence about the book). "
            f"If it is NOT ambiguous (it's a well-known unique title), return: "
            f"  'needs_clarification': false, "
            f"  'resolved_title': the full canonical book title, "
            f"  'resolved_author': the author name. "
            f"Return ONLY valid JSON with no extra text."
        )

        try:
            result = self._query_ai(prompt, api_key, provider)
            if not isinstance(result, dict):  # ai didn't give us the shape we asked for
                raise ValueError("Clarification response wasn't a JSON object")
            return result
        except Exception as e:
            print("All clarification providers failed:", e)
            return {"needs_clarification": False, "resolved_title": book_input, "resolved_author": ""}  # safest fallback: don't block user, just proceed

    def get_ai_recommendations(self, book_title, api_key=None, provider='gemini'):
        # asks ai for exactly 5 recs in strict json format
        prompt = (
            f"You are a helpful and knowledgeable library assistant. Recommend exactly 5 books that are similar, "
            f"closely related, or are a great match for someone who liked '{book_title}'. Provide a combination of same-genre "
            f"and smart cross-genre recommendations. Make sure they are in the same age group and theme of the book."
            f"Return ONLY a JSON object containing a list of objects under the key 'recommendations'. "
            f"Each object must have exactly these keys: "
            f"- 'title': The full title of the recommended book "
            f"- 'author': The author of the recommended book (Lastname, Firstname format preferred, e.g. 'Weir, Andy') "
            f"- 'reason': A 1-sentence explanation of why it fits."
        )

        try:
            data = self._query_ai(prompt, api_key, provider)
            raw_recs = data.get('recommendations', []) if isinstance(data, dict) else []

            recs = []  # keep only well-formed items — ai output isn't guaranteed to match our schema
            for r in raw_recs:
                if not isinstance(r, dict):
                    continue
                title = str(r.get('title', '')).strip()
                author = str(r.get('author', '')).strip()
                if not title or not author:
                    continue
                recs.append({
                    "title": title[:300],
                    "author": author[:200],
                    "reason": str(r.get('reason', '')).strip()[:500]
                })

            return recs if recs else self._recommendation_fallback()
        except Exception as e:
            print("All recommendation providers failed:", e)
            return self._recommendation_fallback()

    @staticmethod
    def _recommendation_fallback():
        return [  # fallback so ui doesn't look broken
            {
                "title": "The Martian",
                "author": "Weir, Andy",
                "reason": "Fallback: A gripping hard science fiction survival story featuring engineering problem-solving."
            },
            {
                "title": "Project Hail Mary",
                "author": "Weir, Andy",
                "reason": "Fallback: A science-heavy, high-stakes journey across space to save humanity."
            }
        ]

    def get_open_library_cover(self, title, author):
        # open library search -> grab cover id or isbn -> build cover url
        query = f"{title} {author}"
        url = f"https://openlibrary.org/search.json?q={urllib.parse.quote(query)}&limit=1"

        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get('docs') and len(data['docs']) > 0:
                    doc = data['docs'][0]

                    cover_id = doc.get('cover_i')  # best case: cover_i exists
                    if cover_id:
                        return f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"

                    isbn_list = doc.get('isbn')  # fallback: try isbn cover endpoint
                    if isbn_list and len(isbn_list) > 0:
                        return f"https://covers.openlibrary.org/b/isbn/{isbn_list[0]}-M.jpg"
        except Exception as e:
            print(f"Failed to fetch cover from Open Library for '{title}':", e)

        return None

    def search_library_catalog(self, title, author, domain, preferred_formats):
        # biblio commons search page (html), not a clean api, so we scrape it
        url = f"https://{domain}/v2/search?query={urllib.parse.quote(title)}&searchType=keyword"

        headers = {  # pretend to be a browser so we don't get blocked instantly
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        }

        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                return []

            soup = BeautifulSoup(r.text, 'html.parser')

            items = soup.find_all('li', class_=re.compile("cp-search-result-item"))  # each search result is a <li> w/ a class like cp-search-result-item...

            matches = []
            for item in items:
                title_elem = item.find(class_='title-content')  # 1) title
                item_title = title_elem.text.strip() if title_elem else ""
                if not item_title:
                    cp_title = item.find(class_='cp-title')  # alt markup path
                    if cp_title:
                        item_title = cp_title.text.strip()

                clean_item_title = item_title.lower()  # quick relevance filter: require some word overlap
                clean_target_title = title.lower()
                if not any(word in clean_item_title for word in clean_target_title.split() if len(word) > 2):
                    continue

                sr_elem = item.find(class_='cp-screen-reader-message')  # 2) format (often buried in screen-reader text)
                sr_text = sr_elem.text.strip() if sr_elem else ""

                item_format = "Book"
                if sr_text and ',' in sr_text:
                    item_format = sr_text.split(',')[-1].strip()

                matched_pref_format = None  # only keep formats user asked for (w/ fuzzy matching)
                for pref in preferred_formats:
                    if pref.lower() in item_format.lower() or item_format.lower() in pref.lower():
                        matched_pref_format = pref
                        break
                if not matched_pref_format:
                    continue

                author_elem = item.find(class_='cp-author-link')  # 3) author
                item_author = author_elem.text.strip() if author_elem else ""

                status_elem = item.find(class_='cp-availability-status')  # 4) availability
                status = status_elem.text.strip() if status_elem else ""
                status_classes = status_elem.get('class', []) if status_elem else []

                is_available = False
                if 'available' in status_classes or 'available' in status.lower():
                    is_available = True
                elif 'unavailable' in status_classes or 'in use' in status.lower():
                    is_available = False
                else:
                    full_block = item.find(class_=re.compile(  # sometimes availability is in a bigger block (esp ebooks)
                        "availability-block|availability_status|cp-availability-bib-block"
                    ))
                    full_text = "".join(full_block.strings).lower() if full_block else ""

                    if 'instantly available' in full_text or 'available now' in full_text:
                        is_available = True
                        status = "Instantly Available (Online)"
                    elif 'copies in use' in full_text or 'copies are in use' in full_text:
                        is_available = False
                        status = "All copies in use"
                    else:
                        status = status or "Check Catalog"

                link_elem = item.find('a', attrs={'data-key': 'bib-title'})  # 5) link to bib record
                link = ""
                if link_elem and link_elem.get('href'):
                    link = f"https://{domain}{link_elem.get('href')}"

                matches.append({
                    "title": item_title,
                    "author": item_author,
                    "format": item_format,
                    "status": status,
                    "is_available": is_available,
                    "link": link
                })

                if len(matches) >= 3:  # keep it tidy: max 3 matches per rec
                    break

            return matches

        except Exception as e:
            print(f"Error scraping catalog for '{title}':", e)
            return []


if __name__ == '__main__':
    mimetypes.init()  # mimetypes can be weird on some systems, so we force a couple common ones
    mimetypes.types_map['.js'] = 'application/javascript'
    mimetypes.types_map['.css'] = 'text/css'

    server_address = ('', PORT)  # bind on all interfaces, PORT
    httpd = http.server.HTTPServer(server_address, LibraryAssistantHandler)

    print(f"Library Assistant Backend Server running on port {PORT}...")
    print(f"Open http://localhost:{PORT} in your browser.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
        httpd.server_close()
