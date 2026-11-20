#!/usr/bin/env python3
"""Fetch a web page by URL and print its readable content. Standard library only.

Usage:
    python3 ask-human-to-fetch.py <URL>

Everything else is decided automatically:
- No scheme given: tries https://, then http://. Name does not resolve: tries "www.".
- Temporary failures (timeouts, dropped connections, HTTP 429/5xx) are retried
  with backoff, honoring Retry-After.
- Certificate verification failures (often a missing local CA bundle) are
  retried without verification, with a note on stderr.
- Blocked requests and bot walls (401/403/429/503, Cloudflare-style challenges)
  are retried in a real headless browser.
- Pages built by JavaScript are rendered in an installed headless browser
  (Chrome, Chromium, Edge, Brave, Vivaldi, Opera, or a Playwright Chromium),
  found automatically. If the first render is empty it waits longer, falls back
  to the old headless mode, then tries the next browser.
- Meta-refresh redirect pages are followed once.
- JSON is pretty-printed, plain text is printed as is, binary files (PDF,
  images, ...) are reported instead of dumped. Very long output is cut.

Exit codes: 0 ok, 1 bad input, 2 HTTP error, 3 network error,
            4 rendering needed but failed, 5 no readable content.
"""

import glob
import gzip
import http.client
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
TIMEOUT = 20            # seconds per network request
ATTEMPTS = 3            # tries per URL for temporary failures
MAX_BYTES = 15_000_000  # stop reading bodies beyond this size
MAX_CHARS = 200_000     # cut printed output beyond this length
RENDER_WAITS = (8, 20)  # seconds of JavaScript time per render attempt
THIN_TEXT = 200         # fewer characters than this counts as "no real content"
RETRY_STATUSES = {429, 500, 502, 503, 504}
BROWSER_WALL_STATUSES = {401, 403, 429, 503}  # often pass in a real browser


def note(msg):
    print(f"Note: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- HTML to text

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "header", "footer", "nav", "aside", "main", "h1", "h2", "h3", "h4", "h5",
    "h6", "pre", "blockquote", "hr", "form", "dd", "dt", "figure", "figcaption",
}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li" and not self.skip_depth:
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip_depth:
            self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n")]
        out, blank = [], False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def html_to_text(html):
    parser = TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep whatever was parsed
        pass
    return re.sub(r"\s+", " ", parser.title).strip(), parser.text()


# ---------------------------------------------------------------- body helpers

def decompress(body, encoding):
    encoding = (encoding or "").lower()
    try:
        if encoding in ("gzip", "x-gzip"):
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        note(f"could not decompress {encoding} body; using it as is")
    return body


def decode(body, content_type):
    charset = None
    m = re.search(r"charset=[\"']?([\w.-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w.-]+)', body[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def classify(ctype, body):
    """Return 'html', 'json', 'text' or 'binary'."""
    ctype = (ctype or "").lower()
    head = body[:512].lstrip().lower()
    if "html" in ctype or head.startswith((b"<!doctype html", b"<html")):
        return "html"
    if "json" in ctype:
        return "json"
    if ctype.startswith("text/") or "xml" in ctype or "javascript" in ctype:
        return "text"
    if b"\x00" in body[:2048]:
        return "binary"
    if not ctype or "octet-stream" in ctype:
        if head.startswith((b"{", b"[")):
            return "json"
        if head.startswith(b"<"):
            return "html"
        return "text"
    return "binary"


JS_MARKERS = re.compile(
    r'id=["\'](root|app|__next|__nuxt|svelte|main-app)["\']|ng-version|data-reactroot'
    r"|enable javascript|requires javascript|javascript is (disabled|required)",
    re.I,
)
CHALLENGE_MARKERS = re.compile(
    r"<title>\s*(just a moment|attention required|access denied|please wait"
    r"|checking your browser|one more step)"
    r"|cf-browser-verification|challenge-platform|_cf_chl_opt|captcha-delivery|px-captcha",
    re.I,
)
META_REFRESH = re.compile(
    r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]*content=[\"']?\s*\d*\s*;?\s*url=([^\"'>\s]+)",
    re.I,
)


def is_thin(text):
    return len(text.strip()) < THIN_TEXT


def is_challenge(html):
    return bool(CHALLENGE_MARKERS.search(html[:20000]))


def looks_js_built(html, text):
    scripts = len(re.findall(r"<script\b", html, re.I))
    if is_thin(text) and scripts:
        return True
    return len(text) < 1500 and bool(JS_MARKERS.search(html))


# ---------------------------------------------------------------- network

class FetchError(Exception):
    def __init__(self, kind, message, code=None):
        super().__init__(message)
        self.kind = kind  # 'http', 'dns' or 'network'
        self.code = code


def retry_delay(err, attempt):
    try:
        value = float(err.headers.get("Retry-After", ""))
        return min(max(value, 0.5), 10)
    except (AttributeError, TypeError, ValueError):
        return 1.5 * (attempt + 1)


def fetch_once(url, ctx):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            body = body[:MAX_BYTES]
            note(f"response is larger than {MAX_BYTES} bytes; only the start was read")
        body = decompress(body, resp.headers.get("Content-Encoding"))
        return resp.geturl(), resp.status, resp.headers.get("Content-Type", ""), body


def fetch(url):
    """Fetch with retries. Returns (final_url, status, content_type, body)."""
    ctx = None
    attempt = 0
    while True:
        try:
            return fetch_once(url, ctx)
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUSES and attempt < ATTEMPTS - 1:
                delay = retry_delay(e, attempt)
                note(f"HTTP {e.code} from {url}; retrying in {delay:.0f}s")
                time.sleep(delay)
                attempt += 1
                continue
            raise FetchError("http", f"HTTP {e.code} {e.reason}", e.code)
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError) and ctx is None:
                note("the site's certificate could not be verified; retrying without verification")
                ctx = ssl._create_unverified_context()
                continue
            if isinstance(reason, socket.gaierror):
                raise FetchError("dns", f"could not resolve host ({reason})")
            if attempt < ATTEMPTS - 1:
                note(f"could not reach {url} ({reason}); retrying")
                time.sleep(1.5 * (attempt + 1))
                attempt += 1
                continue
            raise FetchError("network", f"could not reach {url} ({reason})")
        except (TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException, OSError) as e:
            if attempt < ATTEMPTS - 1:
                note(f"connection problem with {url} ({e or type(e).__name__}); retrying")
                time.sleep(1.5 * (attempt + 1))
                attempt += 1
                continue
            raise FetchError("network", f"could not reach {url} ({e or type(e).__name__})")


def url_variants(raw):
    raw = raw.strip().strip("<>\"'")
    if not raw:
        raise ValueError("no URL given")
    has_scheme = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw) is not None
    url = raw if has_scheme else "https://" + raw.lstrip("/")
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are supported")
    if not p.hostname:
        raise ValueError(f"not a valid URL: {raw}")
    bases = [p]
    host = p.hostname
    is_ip = re.fullmatch(r"[\d.]+|\[?[0-9a-fA-F:]+\]?", host) is not None
    if not host.startswith("www.") and "." in host and not is_ip:
        bases.append(p._replace(netloc="www." + p.netloc))
    variants = []
    for b in bases:
        variants.append(b.geturl())
        if not has_scheme:
            variants.append(b._replace(scheme="http").geturl())
    return variants


# ---------------------------------------------------------------- browser

BROWSER_COMMANDS = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "chrome", "microsoft-edge", "microsoft-edge-stable", "msedge",
    "brave-browser", "brave", "vivaldi", "vivaldi-stable", "opera",
]


def browser_candidates():
    for name in BROWSER_COMMANDS:
        path = shutil.which(name)
        if path:
            yield path
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Darwin":
        apps = [
            "Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser",
            "Vivaldi", "Opera", "Google Chrome Canary",
        ]
        for root in ("/Applications", os.path.join(home, "Applications")):
            for app in apps:
                yield f"{root}/{app}.app/Contents/MacOS/{app}"
        yield from sorted(glob.glob(os.path.join(
            home, "Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium")),
            reverse=True)
    elif system == "Windows":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rels = [
            r"Google\Chrome\Application\chrome.exe",
            r"Chromium\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
            r"Vivaldi\Application\vivaldi.exe",
        ]
        for root in filter(None, roots):
            for rel in rels:
                yield os.path.join(root, rel)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            yield from sorted(glob.glob(os.path.join(
                local, r"ms-playwright\chromium-*\chrome-win*\chrome.exe")), reverse=True)
    else:
        for path in ("/snap/bin/chromium", "/usr/lib/chromium/chromium", "/opt/google/chrome/chrome",
                     "/usr/bin/chromium", "/opt/brave.com/brave/brave", "/opt/microsoft/msedge/msedge"):
            yield path
        yield from sorted(glob.glob(os.path.join(
            home, ".cache/ms-playwright/chromium-*/chrome-linux*/chrome")), reverse=True)


def find_browsers():
    found, seen = [], set()
    for path in browser_candidates():
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            continue
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            found.append(path)
    return found


class RenderError(Exception):
    pass


def run_browser(browser, headless_flag, url, wait_s):
    profile = tempfile.mkdtemp(prefix="ask-human-to-fetch-")
    cmd = [
        browser, headless_flag, "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions", "--mute-audio",
        "--hide-scrollbars", "--window-size=1366,900", f"--user-data-dir={profile}",
        f"--user-agent={USER_AGENT}", f"--virtual-time-budget={int(wait_s * 1000)}",
        "--dump-dom", url,
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.insert(1, "--no-sandbox")  # Chrome refuses to run as root otherwise
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + wait_s + 20)
    except subprocess.TimeoutExpired:
        raise RenderError("timed out")
    except OSError as e:
        raise RenderError(f"could not start ({e})")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    html = proc.stdout.decode("utf-8", "replace")
    if not html.strip():
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RenderError("returned nothing" + (f" ({err[-1][:200]})" if err else ""))
    return html


def render(url):
    """Render url in a headless browser. Returns (html, browser_name)."""
    browsers = find_browsers()
    if not browsers:
        raise RenderError("no Chrome, Chromium, Edge, Brave, Vivaldi or Opera browser is installed")
    errors = []
    for browser in browsers:
        name = os.path.basename(browser)
        for headless_flag in ("--headless=new", "--headless"):
            best = None
            for wait_s in RENDER_WAITS:
                try:
                    html = run_browser(browser, headless_flag, url, wait_s)
                except RenderError as e:
                    errors.append(f"{name}: {e}")
                    break
                best = html
                _, text = html_to_text(html)
                if not is_thin(text) and not is_challenge(html):
                    return html, name
                if wait_s != RENDER_WAITS[-1]:
                    note(f"rendered page is still empty; waiting longer ({RENDER_WAITS[-1]}s)")
            if best is not None:
                return best, name
    raise RenderError("; ".join(errors[-3:]))


# ---------------------------------------------------------------- main

def main():
    args = sys.argv[1:]
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print("Usage: python3 ask-human-to-fetch.py <URL>", file=sys.stderr)
        return 1
    try:
        variants = url_variants(args[0])
    except ValueError as e:
        print(f"Error: {e}.", file=sys.stderr)
        return 1

    result, error, error_url = None, None, variants[0]
    dns_failed = set()
    for url in variants:
        host = urllib.parse.urlparse(url).hostname
        if host in dns_failed:
            continue
        try:
            result = fetch(url)
            break
        except FetchError as e:
            error, error_url = e, url
            if e.kind == "http":
                break
            if e.kind == "dns":
                dns_failed.add(host)
            else:
                note(f"{e}; trying another form of the address")

    final_url, status, ctype, title, text, html = error_url, None, "", "", "", ""
    need_render = False

    if result:
        final_url, status, ctype, body = result
        kind = classify(ctype, body)
        if kind == "binary":
            print(f"Error: {final_url} is a binary file ({ctype or 'unknown type'}, "
                  f"{len(body)} bytes), not a readable page.", file=sys.stderr)
            return 5
        decoded = decode(body, ctype)
        if kind == "json":
            try:
                text = json.dumps(json.loads(decoded), indent=2, ensure_ascii=False)
            except ValueError:
                text = decoded
        elif kind == "text":
            text = decoded
        else:
            html = decoded
            title, text = html_to_text(html)
            refresh = META_REFRESH.search(html[:20000])
            if refresh and is_thin(text):
                target = urllib.parse.urljoin(final_url, refresh.group(1))
                note(f"page redirects to {target}; following it")
                try:
                    final_url, status, ctype, body = fetch(target)
                    html = decode(body, ctype)
                    title, text = html_to_text(html)
                except FetchError as e:
                    note(f"redirect failed ({e})")
            if is_challenge(html):
                note("the site shows a bot check; opening it in a real browser")
                need_render = True
            elif looks_js_built(html, text):
                note("the page seems to be built by JavaScript; rendering it")
                need_render = True
    elif error.kind == "dns":
        print(f"Error: {error} for {variants[0]}.", file=sys.stderr)
        return 3
    elif error.kind == "network" or error.code in BROWSER_WALL_STATUSES:
        note(f"{error}; trying a real browser")
        need_render = True
    else:
        print(f"Error: {error} for {error_url}.", file=sys.stderr)
        return 2

    rendered_by = None
    if need_render:
        try:
            r_html, rendered_by = render(final_url)
            r_title, r_text = html_to_text(r_html)
            if len(r_text) >= len(text) or not result:
                title, text, html = r_title or title, r_text, r_html
                ctype = ctype or "text/html"
            else:
                rendered_by = None  # the plain fetch had more content
        except RenderError as e:
            if not result:
                print(f"Error: {error} for {error_url}; the browser could not load it either ({e}).",
                      file=sys.stderr)
                return 2 if error.kind == "http" else 3
            if is_thin(text):
                print(f"Error: the page needs JavaScript, but rendering failed ({e}).", file=sys.stderr)
                return 4
            note(f"rendering failed ({e}); showing the content that was fetched without it")

    if not text.strip():
        print(f"Error: {final_url} returned no readable content.", file=sys.stderr)
        return 5
    if rendered_by and is_challenge(html):
        note("the page may still be a bot check rather than the real content")

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[...truncated]"

    print(f"URL: {final_url}")
    if status is not None:
        print(f"Status: {status}")
    print(f"Content-Type: {ctype or 'unknown'}")
    print(f"Rendered: {'yes (' + rendered_by + ')' if rendered_by else 'no'}")
    if title:
        print(f"Title: {title}")
    print("-" * 40)
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
