#!/usr/bin/env python3
"""The kit's own engine: the Google Maps scraper's HTTP API and web UI, on this machine only.

    python3 engine/serve.py               # port from SCRAPER_PORT (environment, then .env), default 8080
    python3 engine/serve.py --port 8190

Normally started by `python3 scripts/engine.py start`, or by `docker compose up -d` (the Dockerfile). It speaks the API of the engine the kit used
before (POST/GET/DELETE /api/v1/jobs, /api/v1/jobs/{id}/download, the same job body and "Status"
values), so scripts/scrape.py, the skill and the slash commands work unchanged; jobs also carry Error
and Progress, resume after a restart, and download as CSV or JSON. It listens on
127.0.0.1 (and ::1) only, or on 0.0.0.0 inside the Docker image, where Docker publishes the port on
the host's 127.0.0.1 only. Either way it refuses a foreign Host (DNS rebinding, 421) or Origin (cross-site
posts, 403); with SCRAPER_API_KEY set it also wants that X-API-Key (401). No telemetry: it talks to
Google Maps, for email or socials jobs to the businesses' own websites, and for verify_emails jobs
to Google's public DNS (dns.google), nothing else.
Standard library only.
"""
import argparse, csv, gzip, hmac, html, io, json, os, re, secrets, signal, socket, socketserver, sqlite3, sys, threading, unicodedata, zlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser, jobs  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # ✓ and accented names on a Windows cp1252 console
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

VERSION = "2.5.1"
MARK = f"gmskit-engine/{VERSION}"  # X-Engine header: tells scripts/engine.py this engine from others
REPO = "https://github.com/Mahanaicoach/google-maps-scraper-kit"
# The web UI's sponsor box: the browser reads this static file from GitHub (cached a day, no
# tracking), so the sponsors it lists change without anyone updating their copy of the kit.
SPONSORS = "https://raw.githubusercontent.com/Mahanaicoach/google-maps-scraper-kit/master/assets/sponsors/sponsors.json"
# The web UI's update notice: the browser asks GitHub for the kit's latest release once a day (a
# public JSON endpoint, nothing about the user in the request) and shows a line when it is newer
# than this copy. Nothing updates itself: the line says to run scripts/update.sh.
RELEASES = "https://api.github.com/repos/Mahanaicoach/google-maps-scraper-kit/releases/latest"
# The web UI's What and Where boxes offer only these, so nobody searches for a business type or a
# place that does not exist: presets/business-types.json (hand-picked; add a niche there) and
# presets/cities.tsv.gz (GeoNames cities1000, CC BY 4.0: name, region, country code, lat, lon, biggest first).
# What also offers Google Business Profile's category names (4,053, US English) from
# lobstrio/google-business-categories, pinned to one commit. The browser loads that list from GitHub,
# like the sponsor box; the kit ships no copy, because that repository has no licence file.
CATEGORIES = ("https://raw.githubusercontent.com/lobstrio/google-business-categories/"
              "4ee1c4788d07c812a32986177c2a51c28957062f/data/google-business-categories.txt")
PRESETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")


def load_types(folder=None):
    """The What list; [] with one warning when the file is missing or broken, so the API and the scripts
    still work (only the web UI's What box is empty)."""
    try:
        with open(os.path.join(folder or PRESETS, "business-types.json"), encoding="utf-8") as f:
            types = json.load(f)
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            raise ValueError("not a list of names")
        return types
    except (OSError, ValueError) as e:
        print(f"presets/business-types.json unusable ({e.__class__.__name__}): the What box will be empty", file=sys.stderr)
        return []


TYPES = load_types()
TYPES_JS = json.dumps(TYPES).replace("</", "<\\/")  # the What list, inlined in the page script
_cities, _cities_lock = None, threading.Lock()
ENV_KEYS = ("SCRAPER_PORT", "SCRAPER_API_KEY", "SCRAPER_HEADLESS")
MAX_BODY = 1 << 20
LOCAL = r"(localhost|127\.0\.0\.1|\[::1\])"
HOST_OK = re.compile(rf"^{LOCAL}(:\d{{1,5}})?$", re.I)
ORIGIN_OK = re.compile(rf"^https?://{LOCAL}(:\d{{1,5}})?$", re.I)
JOB_PATH = re.compile(r"^/api/v1/jobs/([0-9a-f-]{36})(/download)?$")
JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_log_lock = threading.Lock()
_REPLIED = object()  # body() answered the request itself (a bad body)


def file_name(job, jid):
    """"bakery-in-portland-or-c063a815": the job's search, then the start of its id; the web UI names
    its downloads the same way (fileName in PAGE)."""
    s = unicodedata.normalize("NFKD", (job or {}).get("Name") or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return (re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "results") + "-" + jid[:8]


FOLD = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ı": "i", "ß": "ss", "æ": "ae", "œ": "oe", "þ": "th"})


def fold(s):
    """"sao paulo sp br" for "São Paulo, SP, BR": accents dropped (also ł, ø, ı...), lower case, punctuation
    as single spaces."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower().translate(FOLD)) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def cities():
    """Every city as one "folded label<TAB>label<TAB>lat<TAB>lon<TAB>country" line, biggest first. Read on the
    first lookup, then kept (about 35 MB)."""
    global _cities
    with _cities_lock:
        if _cities is None:
            rows = []
            try:
                with gzip.open(os.path.join(PRESETS, "cities.tsv.gz"), "rt", encoding="utf-8") as f:
                    for line in f:
                        name, admin, cc, lat, lon = line.rstrip("\n").split("\t")
                        label = ", ".join([name] + ([admin] if admin and admin != name else []) + [cc])
                        rows.append("\t".join((fold(label), label, lat, lon, cc)))
            except (OSError, EOFError, ValueError, zlib.error) as e:  # missing, truncated, corrupt or odd file: what was read, once
                log(f"presets/cities.tsv.gz unusable after {len(rows)} cities ({e.__class__.__name__})")
            _cities = rows
    return _cities


def find_cities(q, limit=20):
    """The biggest cities whose "name, region, country" starts with q; with no q, the biggest in the US."""
    raw, q, out = q.strip(), fold(q), []
    if raw and not q:  # punctuation only: nothing can start with it
        return out
    for row in cities():  # ponytail: a linear scan, about 30 ms; a prefix index if lookups ever lag
        if row.startswith(q) if q else row.endswith("\tUS"):
            _, label, lat, lon, cc = row.split("\t")
            out.append({"label": label, "lat": lat, "lon": lon, "country": cc})
            if len(out) == limit:
                break
    return out


def log(msg):
    """One line on stderr with a UTC timestamp and its offset. Counts only, never scraped values.
    A log that cannot be written (full disk, closed stderr) is dropped, never raised."""
    with _log_lock:
        try:
            print(f"{jobs.utc_now()} {msg}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass


def env_value(v):
    """A .env value: quotes stripped, a trailing " # comment" dropped unless it sits inside the quotes
    (then docker compose keeps it too). Same rule as scripts/scrape.py."""
    v = v.strip()
    if v[:1] in ("\"", "'") and v[0] in v[1:]:
        return v[1:v.index(v[0], 1)]
    return v.split(" #", 1)[0].strip()


def read_env():
    """SCRAPER_PORT, SCRAPER_API_KEY and SCRAPER_HEADLESS: the environment wins, then the kit's .env (last line wins,
    as in docker compose and scripts/scrape.py)."""
    found = {}
    try:
        with open(os.path.join(browser.KIT, ".env"), encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                k, sep, v = line.strip().removeprefix("export ").partition("=")
                if sep and k.strip() in ENV_KEYS:
                    found[k.strip()] = env_value(v)
    except OSError:
        pass
    return {k: os.environ.get(k, found.get(k, "")) for k in ENV_KEYS}


JOB_PROPS = {
    "name": {"type": "string", "description": "any label"},
    "keywords": {"type": "array", "items": {"type": "string"}, "description": "searches, run one after another"},
    "lang": {"type": "string", "description": "2-letter interface language, e.g. en (category names, hours text)"},
    "country": {"type": "string", "description": "2-letter country Google ranks and formats results for, e.g. us "
                                                 "(default: the country of this machine's IP)"},
    "lat": {"type": "string", "description": "the map's centre, e.g. \"30.2672\"; send lat and lon both, or neither "
                                             "(then Google places the search by the keyword, e.g. \"cafes in Austin TX\")"},
    "lon": {"type": "string", "description": "e.g. \"-97.7431\""},
    "max_time": {"type": "integer", "description": "seconds"},
    "depth": {"type": "integer", "description": "result batches per keyword, about 20 places each (default 1)"},
    "zoom": {"type": "integer", "description": "map zoom 1-21 (default 15)"},
    "email": {"type": "boolean", "description": "visit each website's homepage for emails"},
    "verify_emails": {"type": "boolean", "description": "with email: check each address's domain over DNS (dns.google), "
                                                       "drop domains that do not exist, add email_host"},
    "browser_sites": {"type": "boolean", "description": "optional Playwright retry when HTTP finds no email or social links; adds website_fetch_status"},
    "socials": {"type": "boolean", "description": "visit each website's homepage for Instagram, Facebook, LinkedIn, "
                                                 "TikTok, YouTube and X links"},
    "reviews_max": {"type": "integer", "description": "0-5 reviews per place from its panel, one page load per place (headless supported)"},
    "proxies": {"type": "array", "items": {"type": "string"},
                "description": "http(s)://[user:pass@]host:port or socks5://host:port; one per keyword in turn, "
                               "and the next one when Google refuses a keyword"},
    "radius": {"type": "integer", "description": "accepted, not used"},
    "fast_mode": {"type": "boolean", "description": "accepted, not used"},
}
OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Google Maps Scraper Kit engine", "version": VERSION,
             "description": "Local only. Create a job, poll its Status until ok or failed (Progress says how far "
                            "it is), download the CSV or JSON. A job the engine was stopped in the middle of "
                            f"resumes from its last finished keyword on the next start. Source and docs: {REPO}"},
    "paths": {
        "/api/v1/jobs": {
            "get": {"summary": "List jobs, newest first (null when there are none)"},
            "post": {"summary": "Create a job; returns 201 {\"id\": \"<uuid>\"}, or 422 {\"code\", \"message\"}",
                     "requestBody": {"content": {"application/json": {"schema": {
                         "type": "object", "required": ["name", "keywords", "lang", "max_time"],
                         "properties": JOB_PROPS}}}}}},
        "/api/v1/jobs/{id}": {
            "get": {"summary": "One job: ID, Name, Date, Status (pending, working, ok, failed), Data (proxy "
                               "passwords masked), Error, Progress (keywords_done, keywords_total, places, percent, "
                               "stage: queued, search, reviews, websites or done)"},
            "delete": {"summary": "Delete a job and its results (stops it if it is running)"}},
        "/api/v1/jobs/{id}/download": {"get": {"summary": "The job's CSV, the rows so far while it works; "
                                                          "?format=leads for the 8 lead fields and socials with junk "
                                                          "emails dropped, as scripts/scrape.py writes them; "
                                                          "?format=json for a JSON array of the same rows"}},
        "/api/v1/cities": {"get": {"summary": "Up to 20 cities for the web UI's Where box, each {label, lat, lon, "
                                              "country}: ?q=aus gives the biggest whose \"name, region, country\" "
                                              "starts with it, no q the biggest US cities (GeoNames, CC BY 4.0)"}},
        "/api/openapi.json": {"get": {"summary": "This description as OpenAPI JSON (the page at /api/docs shows it)"}},
    },
}


def docs_page(nonce):
    """/api/docs: OPENAPI as a plain HTML page (no scripts, nothing loaded from elsewhere)."""
    e = html.escape
    out = [f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Engine API</title>"
           f"<style nonce=\"{nonce}\">:root {{ color-scheme: light dark; }} body {{ margin: 0 auto; max-width: 960px; "
           "padding: 16px; font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; }} code {{ font-size: .95em; }} "
           "table {{ width: 100%; border-collapse: collapse; }} th, td {{ text-align: left; vertical-align: top; "
           "padding: 4px 6px; border-bottom: 1px solid #8884; }} .m {{ font-weight: 600; }}</style></head><body>",
           f"<h1>{e(OPENAPI['info']['title'])} {e(VERSION)}</h1><p>{e(OPENAPI['info']['description'])}</p>",
           "<p><a href=\"/\">Web UI</a> · <a href=\"/api/openapi.json\">OpenAPI JSON</a> · "
           "With SCRAPER_API_KEY set, send it as an <code>X-API-Key</code> header.</p>"]
    for path, ops in OPENAPI["paths"].items():
        for method, op in ops.items():
            out.append(f"<h2><span class=\"m\">{e(method.upper())}</span> <code>{e(path)}</code></h2><p>{e(op['summary'])}</p>")
            schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            if schema:
                out.append("<table><tr><th>Field</th><th>Type</th><th>Required</th><th>Meaning</th></tr>")
                for name, prop in schema["properties"].items():
                    out.append(f"<tr><td><code>{e(name)}</code></td><td>{e(prop['type'])}</td>"
                               f"<td>{'yes' if name in schema['required'] else ''}</td>"
                               f"<td>{e(prop.get('description', ''))}</td></tr>")
                out.append("</table>")
    out.append("</body></html>")
    return "".join(out)


PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maps Scraper</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 24 24%27%3E%3Cpath fill=%27%2310b981%27 d=%27M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 7-13a7 7 0 0 0-7-7zm0 9.5a2.5 2.5 0 1 1 0-5 2.5 2.5 0 0 1 0 5z%27/%3E%3C/svg%3E">
<style nonce="{nonce}">
:root {{ color-scheme: dark; --bg: #09090b; --surface: #18181b; --surface2: #27272a; --text: #fafafa; --muted: #a1a1aa;
  --border: #27272a; --accent: #10b981; --accent-dim: rgba(16,185,129,.12); --accent-hover: #34d399; --danger: #ef4444;
  --warn: #f59e0b; --radius: 12px;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
* {{ box-sizing: border-box; margin: 0; }}
body {{ background: var(--bg); color: var(--text); font: 15px/1.6 var(--sans); -webkit-font-smoothing: antialiased; }}
main {{ max-width: 640px; margin: 0 auto; padding: 32px 20px 80px; }}
a {{ color: var(--accent); text-decoration: none; }} a:hover {{ color: var(--accent-hover); }}

.header {{ padding: 0 0 32px; text-align: center; }}
.header h1 {{ font-size: 1.5rem; font-weight: 700; letter-spacing: -.02em; }}
.header h1 span {{ color: var(--accent); }}
.header p {{ color: var(--muted); font-size: .85rem; margin-top: 4px; }}

.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 20px; }}
.card-title {{ font-size: .75rem; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 16px; }}

.field {{ margin-bottom: 16px; }}
.field:last-child {{ margin-bottom: 0; }}
.field-label {{ display: block; font-size: .8rem; font-weight: 600; color: var(--muted); margin-bottom: 6px; text-transform: uppercase; letter-spacing: .05em; }}
.input-wrap {{ display: flex; align-items: center; gap: 10px; background: var(--bg); border: 1px solid var(--border);
  border-radius: 10px; padding: 0 14px; transition: border-color .15s; }}
.input-wrap:focus-within {{ border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }}
.input-wrap svg {{ flex: none; color: var(--muted); }}
.input-wrap input {{ flex: 1; background: none; border: 0; outline: 0; color: var(--text); font: inherit; padding: 12px 0; }}
.input-wrap input::placeholder {{ color: var(--muted); opacity: .6; }}
textarea#proxies:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }}

.tags {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.tags:empty {{ display: none; }}
.tag {{ display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; background: var(--accent-dim);
  color: var(--accent); border: 1px solid transparent; border-radius: 20px; font-size: .8rem; font-weight: 500; cursor: pointer; }}
.tag:hover {{ border-color: var(--accent); }}

.opts {{ position: absolute; z-index: 10; left: 0; right: 0; top: calc(100% + 4px); max-height: 240px; overflow-y: auto;
  padding: 4px; list-style: none; background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: 0 16px 48px rgba(0,0,0,.4); }}
.opts li {{ padding: 10px 12px; border-radius: 8px; cursor: pointer; font-size: .9rem; }}
.opts li:hover, .opts li[aria-selected=true] {{ background: var(--accent-dim); color: var(--accent); }}
.box {{ position: relative; }}

.toggles {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.toggle {{ display: flex; align-items: center; gap: 8px; padding: 8px 14px; background: var(--bg); border: 1px solid var(--border);
  border-radius: 8px; font-size: .85rem; cursor: pointer; transition: all .15s; user-select: none; }}
.toggle:hover {{ border-color: var(--muted); }}
.toggle:has(input:checked) {{ border-color: var(--accent); background: var(--accent-dim); color: var(--accent); }}
.toggle input {{ display: none; }}
.toggle .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--muted); transition: background .15s; }}
.toggle:has(input:checked) .dot {{ background: var(--accent); }}

.advanced-toggle {{ display: flex; align-items: center; justify-content: space-between; padding: 12px 0;
  color: var(--muted); font-size: .85rem; font-weight: 500; cursor: pointer; border: 0; background: 0; width: 100%; text-align: left; }}
.advanced-toggle:hover {{ color: var(--text); }}
.advanced-toggle svg {{ transition: transform .2s; }}
.advanced-body {{ display: none; padding-bottom: 8px; }}
.advanced-body.open {{ display: grid; gap: 12px; grid-template-columns: 1fr 1fr; }}
.advanced-body label {{ display: grid; gap: 4px; font-size: .8rem; color: var(--muted); font-weight: 500; }}
.advanced-body input {{ background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
  color: var(--text); font: .85rem var(--mono); outline: 0; }}
.advanced-body input:focus {{ border-color: var(--accent); }}
.advanced-body textarea {{ grid-column: 1 / -1; background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; color: var(--text); font: .8rem/1.5 var(--mono); min-height: 70px; resize: vertical; outline: 0; }}
.advanced-body textarea:focus {{ border-color: var(--accent); }}
.advanced-body .wide {{ grid-column: 1 / -1; }}

.submit {{ display: flex; align-items: center; gap: 12px; margin-top: 4px; }}
.btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 12px 28px; border: 0;
  border-radius: 10px; font: 600 .9rem var(--sans); cursor: pointer; transition: all .15s; }}
.btn-primary {{ background: var(--accent); color: #000; }}
.btn-primary:hover {{ background: var(--accent-hover); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(16,185,129,.3); }}
.btn:disabled {{ opacity: .5; cursor: not-allowed; transform: none; box-shadow: none; }}
#msg {{ font-size: .85rem; }} #msg.ok {{ color: var(--accent); }} #msg.bad {{ color: var(--danger); }}

.job {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px 20px;
  margin-bottom: 8px; transition: border-color .15s; }}
.job:hover {{ border-color: var(--muted); }}
.job-top {{ display: flex; align-items: start; justify-content: space-between; gap: 12px; }}
.job-name {{ font-weight: 600; font-size: .95rem; }}
.job-time {{ color: var(--muted); font: .75rem var(--mono); white-space: nowrap; }}
.job-status {{ display: flex; align-items: center; gap: 8px; margin-top: 8px; font-size: .8rem; }}
.job-dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--muted); flex: none; }}
.job-dot.ok {{ background: var(--accent); }} .job-dot.working {{ background: var(--warn); }} .job-dot.failed {{ background: var(--danger); }}
.job-detail {{ color: var(--muted); font-family: var(--mono); font-size: .75rem; }}
.job-error {{ color: var(--danger); font-size: .8rem; margin-top: 6px; }}
.job-bar {{ width: 100%; max-width: 180px; height: 4px; margin-top: 8px; border-radius: 2px; background: var(--border);
  overflow: hidden; }}
.job-bar-fill {{ height: 100%; background: var(--accent); border-radius: 2px; transition: width .3s; }}
.job-actions {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
.btn-sm {{ padding: 6px 12px; font-size: .75rem; font-weight: 500; border-radius: 6px; background: var(--surface2);
  border: 1px solid var(--border); color: var(--text); cursor: pointer; transition: all .15s; }}
.btn-sm:hover {{ border-color: var(--accent); color: var(--accent); }}
.btn-sm.danger:hover {{ border-color: var(--danger); color: var(--danger); }}
.empty {{ text-align: center; padding: 32px; color: var(--muted); font-size: .9rem; }}

[hidden] {{ display: none !important; }}
.sr {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }}
label#keybox {{ display: none; font-size: .85rem; color: var(--muted); }}
label#keybox[style] {{ display: grid; gap: 6px; margin-top: 12px; }}

@media (max-width: 480px) {{
  main {{ padding: 20px 16px 60px; }}
  .advanced-body.open {{ grid-template-columns: 1fr; }}
  .btn {{ width: 100%; }}
}}
</style></head><body><main>

<div class="header">
  <h1><span>&#9679;</span> Maps Scraper</h1>
  <p>v{version}</p>
</div>

<div class="card">
  <div class="card-title">Nova busca</div>
  <form id="f">
    <div class="field"><span class="field-label">Tipo de neg&oacute;cio</span>
      <div class="box"><div class="input-wrap">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
        <input id="what" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="what-opts" autocomplete="off" placeholder="Dentistas, Restaurantes...">
      </div><ul class="opts" id="what-opts" role="listbox" aria-label="Tipos de negócio" hidden></ul></div>
      <div class="tags" id="whats"></div>
    </div>

    <div class="field"><span class="field-label">Cidade</span>
      <div class="box"><div class="input-wrap">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg>
        <input id="where" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="where-opts" autocomplete="off" placeholder="S&atilde;o Paulo, Curitiba...">
      </div><ul class="opts" id="where-opts" role="listbox" aria-label="Cidades" hidden></ul></div>
      <div class="tags" id="wheres"></div>
    </div>

    <div class="field"><span class="field-label">Coletar</span>
      <div class="toggles">
        <label class="toggle"><input type="checkbox" id="email" checked><span class="dot"></span> E-mails</label>
        <label class="toggle"><input type="checkbox" id="socials" checked><span class="dot"></span> Redes sociais</label>
        <label class="toggle"><input type="checkbox" id="browser-sites"><span class="dot"></span> Renderizar sites</label>
        <label class="toggle"><input type="checkbox" id="verify"><span class="dot"></span> Verificar MX</label>
      </div>
    </div>

    <div class="field">
      <button type="button" class="advanced-toggle" id="adv-btn" aria-expanded="false">
        Op&ccedil;&otilde;es avan&ccedil;adas
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div class="advanced-body" id="adv-body">
        <label>Profundidade <input id="depth" type="number" min="1" max="1000" value="5"></label>
        <label>Tempo limite (s) <input id="max_time" type="number" min="1" max="86400" value="600"></label>
        <label>Zoom <input id="zoom" type="number" min="1" max="21" value="15"></label>
        <label>Avalia&ccedil;&otilde;es/local <input id="reviews" type="number" min="0" max="5" value="0"></label>
        <label>Idioma <input id="lang" value="pt" maxlength="2"></label>
        <label>Pa&iacute;s <input id="country" value="br" maxlength="2" placeholder="auto"></label>
      </div>
    </div>

    <div class="field"><span class="field-label">Proxies</span>
      <textarea id="proxies" rows="2" placeholder="http://user:pass@proxy:8000&#10;Um por linha" style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:12px 14px;color:var(--text);font:.85rem/1.5 var(--mono);resize:vertical;width:100%;outline:0;"></textarea>
    </div>

    <div class="submit">
      <button class="btn btn-primary" id="go" type="submit">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        Iniciar busca
      </button>
      <p id="msg" role="status" aria-live="polite"></p>
    </div>
    <label id="keybox" for="key">Chave da API<input id="key" type="password" autocomplete="off"></label>
  </form>
</div>

<div class="card">
  <div class="card-title">Tarefas</div>
  <div id="jobs"></div>
</div>

</main>
<script nonce="{nonce}">
const $ = id => document.getElementById(id);

// Advanced toggle
$('adv-btn').addEventListener('click', () => {{
  const body = $('adv-body'), open = body.classList.toggle('open');
  $('adv-btn').setAttribute('aria-expanded', open);
  $('adv-btn').querySelector('svg').style.transform = open ? 'rotate(180deg)' : '';
}});

let key = ''; try {{ key = localStorage.getItem('gmskit-key') || ''; }} catch (e) {{}}
$('key').value = key;
$('key').addEventListener('change', () => {{ key = $('key').value; try {{ localStorage.setItem('gmskit-key', key); }} catch (e) {{}} load(); }});
function api(method, path, body) {{
  const h = {{}}; if (key) h['X-API-Key'] = key; if (body) h['Content-Type'] = 'application/json';
  return fetch(path, {{method, headers: h, body: body ? JSON.stringify(body) : undefined}}).then(r => {{
    if (r.status === 401) {{ $('keybox').style.display = 'grid'; throw new Error('Chave da API necessária.'); }}
    return r;
  }});
}}
function say(t, kind) {{ $('msg').textContent = t; $('msg').className = kind || ''; }}
function fileName(j) {{
  const s = (j.Name || '').normalize('NFKD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
  return (s || 'results') + '-' + j.ID.slice(0, 8);
}}
async function download(j, fmt) {{
  const r = await api('GET', '/api/v1/jobs/' + j.ID + '/download' + (fmt === 'csv' ? '' : '?format=' + fmt));
  if (!r.ok) return say('Sem resultados ainda.', 'bad');
  const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob());
  a.download = fileName(j) + (fmt === 'leads' ? '-leads.csv' : '.' + fmt); document.body.appendChild(a); a.click(); a.remove();
}}
const LABEL = {{pending: 'aguardando', working: 'em andamento', ok: 'concluído', failed: 'falhou'}};
function progress(j) {{
  const p = j.Progress || {{}}, name = LABEL[j.Status] || j.Status;
  if (j.Status === 'pending') return p.keywords_done ? name + ' (retoma na palavra ' + (p.keywords_done + 1) + ')' : name;
  if (j.Status !== 'working') return name + (p.places ? ' · ' + p.places + ' locais' : '');
  const stage = {{search: 'palavra ' + Math.min(p.keywords_done + 1, p.keywords_total) + ' de ' + p.keywords_total,
    reviews: 'lendo avaliações' + (p.reviews_total ? ' ' + p.reviews_done + ' de ' + p.reviews_total : ''),
    websites: 'verificando sites'}}[p.stage] || p.stage || '';
  return name + ' · ' + stage + ' · ' + (p.places || 0) + ' locais';
}}
async function remove(j) {{
  if (!confirm('Excluir esta tarefa?')) return;
  const r = await api('DELETE', '/api/v1/jobs/' + j.ID); say(r.ok ? 'Excluído.' : 'Erro.', r.ok ? 'ok' : 'bad'); load();
}}
async function load() {{
  try {{
    const list = await (await api('GET', '/api/v1/jobs')).json() || [];
    const box = $('jobs'); box.replaceChildren();
    if (!list.length) {{ box.innerHTML = '<div class="empty">Nenhuma tarefa ainda</div>'; return; }}
    for (const j of list) {{
      const d = document.createElement('div'); d.className = 'job';
      const when = new Date(j.Date);
      const timeStr = isNaN(when) ? j.Date : when.toLocaleString([], {{dateStyle: 'short', timeStyle: 'short'}});
      const [word, ...more] = progress(j).split(' · ');
      let html = '<div class="job-top"><span class="job-name">' + (j.Name || '').replace(/</g,'&lt;') + '</span>'
        + '<span class="job-time">' + timeStr + '</span></div>'
        + '<div class="job-status"><span class="job-dot ' + j.Status + '"></span>' + word + '</div>';
      if (more.length) html += '<div class="job-detail">' + more.join(' · ') + '</div>';
      if (j.Error) html += '<div class="job-error">' + j.Error.replace(/</g,'&lt;') + '</div>';
      if (j.Status === 'working') {{
        const pct = (j.Progress || {{}}).percent || 0;
        html += '<div class="job-bar"><div class="job-bar-fill" style="width:' + pct + '%"></div></div>';
      }}
      html += '<div class="job-actions">';
      if (j.Status !== 'pending' || (j.Progress || {{}}).places)
        html += '<button class="btn-sm" data-dl="leads">Leads CSV</button>'
              + '<button class="btn-sm" data-dl="csv">Full CSV</button>'
              + '<button class="btn-sm" data-dl="json">JSON</button>';
      html += '<button class="btn-sm danger" data-del="1">Excluir</button></div>';
      d.innerHTML = html;
      d.querySelectorAll('[data-dl]').forEach(b => b.addEventListener('click', () => download(j, b.dataset.dl)));
      d.querySelector('[data-del]').addEventListener('click', () => remove(j));
      box.appendChild(d);
    }}
  }} catch (e) {{ say(e.message, 'bad'); }}
}}
const TYPES = {types};
const whats = [], wheres = [];
function tags(box, list) {{
  box.replaceChildren();
  list.forEach((v, i) => {{
    const b = document.createElement('button'), text = v.label || v; b.type = 'button'; b.className = 'tag';
    b.textContent = text + ' ×'; b.setAttribute('aria-label', 'Remove ' + text);
    b.addEventListener('click', () => {{ list.splice(i, 1); tags(box, list); }}); box.appendChild(b);
  }});
}}
function combo(input, box, source, choose) {{
  let items = [], active = -1, stale = false;
  function show(list) {{
    items = list; active = list.length ? 0 : -1; stale = false; box.replaceChildren();
    list.forEach((it, i) => {{
      const li = document.createElement('li'); li.id = box.id + '-' + i; li.setAttribute('role', 'option'); li.textContent = it.label;
      li.addEventListener('mousedown', ev => {{ ev.preventDefault(); pick(i); }}); box.appendChild(li);
    }});
    mark(); box.hidden = !list.length; input.setAttribute('aria-expanded', String(!box.hidden));
  }}
  function mark() {{
    [...box.children].forEach((li, i) => li.setAttribute('aria-selected', String(i === active)));
    if (active < 0) return input.removeAttribute('aria-activedescendant');
    input.setAttribute('aria-activedescendant', box.id + '-' + active); box.children[active].scrollIntoView({{block: 'nearest'}});
  }}
  function pick(i) {{ choose(items[i]); input.value = ''; say('', ''); source('', show); }}
  input.addEventListener('input', () => {{
    items = []; active = -1; stale = true; mark(); if ($('msg').className === 'bad') say('', ''); source(input.value.trim(), show); }});
  input.addEventListener('focus', () => source(input.value.trim(), show));
  input.addEventListener('blur', () => show([]));
  input.addEventListener('keydown', ev => {{
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {{
      ev.preventDefault(); if (!items.length) return;
      active = (active + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; mark();
    }} else if (ev.key === 'Enter') {{
      ev.preventDefault();
      if (active >= 0) pick(active);
      else if (input.value.trim() && !stale) say('Escolha da lista.', 'bad');
    }} else if (ev.key === 'Escape') show([]);
  }});
}}
combo($('what'), $('what-opts'), (q, show) => {{
  q = q.toLowerCase();
  const hit = TYPES.filter(t => t.toLowerCase().includes(q)), first = hit.filter(t => t.toLowerCase().startsWith(q));
  show(first.concat(hit.filter(t => !first.includes(t))).slice(0, 50).map(t => ({{label: t}})));
}}, t => {{ if (!whats.includes(t.label)) whats.push(t.label); tags($('whats'), whats); }});
(function () {{
  const key = 'gmskit-categories', month = 30 * 86400000, base = s => s.toLowerCase().replace(/s$/, '');
  function add(names) {{
    const have = new Set(TYPES.map(base));
    for (const n of names) if (!have.has(base(n))) {{ have.add(base(n)); TYPES.push(n); }}
  }}
  try {{
    const c = JSON.parse(localStorage.getItem(key) || 'null');
    if (c && Date.now() - c.t < month && Array.isArray(c.names)) return add(c.names);
  }} catch (e) {{}}
  fetch('{categories}', {{credentials: 'omit'}}).then(r => r.ok ? r.text() : '').then(t => {{
    const names = t.split('\\n').map(s => s.trim()).filter(s => s && s.length <= 80).slice(0, 5000);
    if (!names.length) return;
    add(names);
    try {{ localStorage.setItem(key, JSON.stringify({{t: Date.now(), names}})); }} catch (e) {{}}
  }}).catch(() => {{}});
}})();
let timer = 0;
combo($('where'), $('where-opts'), (q, show) => {{
  clearTimeout(timer);
  timer = setTimeout(async () => {{
    try {{
      const r = await api('GET', '/api/v1/cities?q=' + encodeURIComponent(q)); if (!r.ok) return;
      const found = await r.json();
      if (q === $('where').value.trim() && document.activeElement === $('where')) show(found);
    }} catch (e) {{ say(e.message, 'bad'); }}
  }}, 120);
}}, c => {{ if (!wheres.some(w => w.label === c.label)) wheres.push(c); tags($('wheres'), wheres); }});
$('f').addEventListener('submit', async ev => {{
  ev.preventDefault();
  for (const box of [$('what'), $('where')]) if (box.value.trim()) {{ box.focus();
    return say('Escolha da lista primeiro.', 'bad'); }}
  if (!whats.length || !wheres.length) {{ (whats.length ? $('where') : $('what')).focus();
    return say('Escolha um tipo de negócio e uma cidade.', 'bad'); }}
  const proxies = $('proxies').value.split('\\n').map(s => s.trim()).filter(Boolean);
  const go = $('go'); go.disabled = true;
  let started = 0;
  try {{
    for (const c of wheres) {{
      const keywords = whats.map(w => w + ' in ' + c.label);
      const body = {{name: Array.from(keywords[0] + (keywords.length > 1 ? ' +' + (keywords.length - 1) + ' more' : '')).slice(0, 200).join(''),
        keywords, lang: $('lang').value.trim().toLowerCase(), country: $('country').value.trim().toLowerCase() || c.country.toLowerCase(),
        lat: c.lat, lon: c.lon, zoom: +$('zoom').value || 15, depth: +$('depth').value, max_time: +$('max_time').value,
        email: $('email').checked, socials: $('socials').checked, browser_sites: $('browser-sites').checked, verify_emails: $('verify').checked,
        reviews_max: +$('reviews').value || 0}};
      if (proxies.length) body.proxies = proxies;
      const r = await api('POST', '/api/v1/jobs', body), d = await r.json();
      if (!r.ok) return say(d.message, 'bad');
      started++;
    }}
    say(started === 1 ? 'Iniciado! Acompanhe abaixo.' : started + ' tarefas iniciadas.', 'ok');
  }} catch (e) {{ say(e.message, 'bad'); }}
  finally {{ go.disabled = false; load(); }}
}});
load(); setInterval(load, 3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gmskit-engine"
    sys_version = ""
    timeout = 30  # a client that stalls mid-body or idles on keep-alive would otherwise pin a thread for good

    def log_message(self, fmt, *args):  # errors are logged by reply(); polls would flood the log
        pass

    def reply(self, code, body=b"", ctype="application/json", headers=()):
        if ctype == "application/json":
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Engine", MARK)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        if self.close_connection:  # tell the client too, so it never reuses the socket
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        if code >= 400:
            log(f"{self.command} {self.path.split('?')[0]!r} -> {code}")  # repr: no control characters in the log

    def fail(self, code, message):
        if self.command != "GET":  # a refused POST or DELETE may leave its body unread on the socket
            self.close_connection = True
        self.reply(code, {"code": code, "message": message})

    def guard(self):
        """Host (DNS rebinding) and Origin (cross-site posts) checks, plus the optional key."""
        host = self.headers.get("Host", "")
        if not HOST_OK.match(host):
            self.close_connection = True
            self.fail(421, "this engine answers to localhost only")
            return False
        origin = self.headers.get("Origin")  # only this page: a dev server on another local port is another site
        if origin is not None and not (ORIGIN_OK.match(origin) and origin.split("://", 1)[1].lower() == host.lower()):
            self.close_connection = True
            self.fail(403, "requests from other websites are refused")
            return False
        key, path = self.server.api_key, self.path.split("?")[0]
        if key and path not in ("/", "/api/docs", "/api/openapi.json") and not hmac.compare_digest(
                self.headers.get("X-API-Key", "").encode("latin-1", "replace"), key.encode()):  # headers arrive as latin-1
            self.fail(401, "missing or wrong X-API-Key")
            return False
        return True

    def body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            self.fail(411, "send a Content-Length")
            return _REPLIED
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            self.close_connection = True
            self.fail(413 if n > MAX_BODY else 400, "the body must be 1 MB or less")
            return _REPLIED
        try:
            return json.loads(self.rfile.read(n) or b"null")
        except (ValueError, RecursionError):  # bad JSON, bad UTF-8, or nested too deep to parse
            self.fail(400, "the body is not valid JSON")
            return _REPLIED

    def download(self, jid, query=""):
        data = self.server.store.read_csv(jid) if JOB_ID.match(jid or "") else None
        name = file_name(self.server.store.get(jid) if data is not None else None, jid or "")
        if data is None:
            return self.fail(404, "no results for this job (yet)")
        fmt = urllib.parse.parse_qs(query).get("format", ["csv"])[0]
        if fmt == "json":  # the same rows, every value a string, as in the CSV
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", "replace"))))
            return self.reply(200, json.dumps(rows, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                              [("Content-Disposition", f'attachment; filename="{name}.json"')])
        if fmt == "leads":
            return self.reply(200, jobs.leads_csv(data), "text/csv; charset=utf-8",
                              [("Content-Disposition", f'attachment; filename="{name}-leads.csv"')])
        if fmt != "csv":
            return self.fail(400, "format must be csv, leads or json")
        self.reply(200, jobs.guard_csv(data), "text/csv; charset=utf-8",  # a spreadsheet opens it: no formulas
                   [("Content-Disposition", f'attachment; filename="{name}.csv"')])

    def do_GET(self):
        if not self.guard():
            return
        path, _, query = self.path.partition("?")
        store = self.server.store
        if path == "/api/v1/jobs":  # null when empty, as before: the docs' health check
            return self.reply(200, [jobs.public(j) for j in store.all()] or None)
        m = JOB_PATH.match(path)
        if m:
            if m.group(2):
                return self.download(m.group(1), query)
            job = store.get(m.group(1))
            return self.reply(200, jobs.public(job)) if job else self.fail(404, "job not found")
        if path == "/download":  # the old web UI's download link
            return self.download(urllib.parse.parse_qs(query).get("id", [""])[0], query)
        if path == "/api/docs":
            nonce = secrets.token_urlsafe(16)
            csp = f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            return self.reply(200, docs_page(nonce).encode(), "text/html; charset=utf-8",
                              [("Content-Security-Policy", csp)])
        if path == "/api/v1/cities":
            return self.reply(200, find_cities(urllib.parse.parse_qs(query).get("q", [""])[0][:100]))
        if path == "/api/openapi.json":
            return self.reply(200, OPENAPI)
        if path == "/":
            nonce = secrets.token_urlsafe(16)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                   "connect-src 'self' https://raw.githubusercontent.com https://api.github.com; img-src 'self' data:; "
                   "form-action 'self'; base-uri 'none'; "
                   "frame-ancestors 'none'")
            page = PAGE.format(nonce=nonce, version=VERSION, repo=REPO, sponsors=SPONSORS, releases=RELEASES, types=TYPES_JS,
                               categories=CATEGORIES).encode()
            return self.reply(200, page, "text/html; charset=utf-8", [("Content-Security-Policy", csp)])
        self.fail(404, "not found")

    do_HEAD = do_GET  # the same headers (X-Engine included), no body

    def do_POST(self):
        if not self.guard():
            return
        if self.path.split("?")[0] != "/api/v1/jobs":
            return self.fail(404, "not found")
        body = self.body()
        if body is _REPLIED:
            return
        try:
            name, data = jobs.validate(body)
        except ValueError as e:
            return self.fail(422, str(e))
        try:
            jid = self.server.store.create(name, data)
        except (UnicodeEncodeError, ValueError) as e:  # validate() keeps these out; a belt for the braces
            return self.fail(422, f"invalid body: {e}")
        except sqlite3.Error as e:  # a full disk or a locked database: answer, do not drop the connection
            return self.fail(500, f"could not store the job: {e}")
        self.server.worker.wake.set()
        log(f"job {jid} created: {len(data['keywords'])} keyword(s)")
        self.reply(201, {"id": jid})

    def do_DELETE(self):
        if not self.guard():
            return
        m = JOB_PATH.match(self.path.split("?")[0])
        if not m or m.group(2):
            return self.fail(404, "not found")
        if not self.server.store.delete(m.group(1)):  # delete first: a job claimed after the cancel sees it is gone
            return self.fail(404, "job not found")
        self.server.worker.cancel(m.group(1))
        log(f"job {m.group(1)} deleted")
        self.reply(200, {"message": "deleted"})


class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would let a second server share the port
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind also calls socket.getfqdn(), a reverse DNS lookup that took over
        # 30 s on a macOS runner before the engine could answer. The name is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def port_taken(port):
    """True if something answers on 127.0.0.1:port. With SO_REUSEADDR, macOS lets a new server bind
    127.0.0.1:port while another listens on *:port (the Docker engine), and takes its traffic."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


class Server6(Server):
    address_family = socket.AF_INET6


def lock_data(folder):
    """An exclusive lock on <folder>/engine.lock, held for the engine's life: a second engine on the same
    data (serve.py --port N by hand) would flip the first one's job to pending and cut its CSV."""
    os.makedirs(folder, exist_ok=True)
    f = open(os.path.join(folder, "engine.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise OSError("its engine.lock is held")
    return f


def in_container():
    """True inside a Docker or Podman container (each leaves its marker file at the root)."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def make_servers(port, store, worker, api_key="", host="127.0.0.1"):
    """The server on host:port. On 127.0.0.1 also one on [::1]:port when this machine has IPv6
    loopback (Windows tries ::1 first for "localhost" and waits ~2 s before falling back)."""
    if port and port_taken(port):
        raise OSError(f"something already listens on port {port}")
    servers = [Server((host, port), Handler)]
    if host == "127.0.0.1":
        try:
            servers.append(Server6(("::1", servers[0].server_address[1]), Handler))
        except OSError:
            pass
    for s in servers:
        s.store, s.worker, s.api_key = store, worker, api_key
    return servers


def main():
    ap = argparse.ArgumentParser(description="The kit's Google Maps engine (local HTTP API and web UI).")
    ap.add_argument("--port", type=int, help="default: SCRAPER_PORT from the environment or .env, else 8080")
    ap.add_argument("--data", default=os.path.join(browser.HOME, "data"), help="jobs and results folder")
    ap.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "0.0.0.0"),
                    help="0.0.0.0 is for the Docker image only, where Docker publishes the port on the "
                         "host's 127.0.0.1 (default: 127.0.0.1)")
    a = ap.parse_args()
    if a.host != "127.0.0.1" and not in_container():
        # On a laptop or server 0.0.0.0 opens the engine to the network: the Host check stops DNS
        # rebinding from a browser, not a machine that sends "Host: localhost" itself.
        sys.exit("✗ --host 0.0.0.0 is for the Docker image only. On this machine the engine listens on "
                 "127.0.0.1, which keeps other machines out.")
    env = read_env()
    try:
        port = a.port or int(env["SCRAPER_PORT"] or 8080)
    except ValueError:
        sys.exit(f"✗ SCRAPER_PORT is not a number: {env['SCRAPER_PORT']!r}")
    browser.make_home()
    browser.sweep_profiles()
    try:
        hold = lock_data(a.data)  # noqa: F841 - held for the engine's life
    except OSError as e:
        sys.exit(f"✗ Another engine already uses {a.data}: {e}. Stop it first (python3 scripts/engine.py stop, "
                 "or docker compose stop).")
    store = jobs.Store(a.data)
    headless = env["SCRAPER_HEADLESS"].strip().lower() not in ("0", "false", "no", "off")
    worker = jobs.Worker(store, log, headless=headless)
    try:
        servers = make_servers(port, store, worker, env["SCRAPER_API_KEY"], a.host)
    except OSError as e:
        sys.exit(f"✗ Cannot listen on {a.host}:{port}: {e}")
    # Only now: an engine that cannot bind (another one runs on this data) must not touch its jobs.
    resumed, failed = store.recover()
    if resumed:
        log(f"{resumed} job(s) were still working when the engine last stopped: they resume from their last "
            "finished keyword")
    if failed:
        log(f"{failed} job(s) stopped mid-run {jobs.MAX_RESUMES + 1} times: marked failed, rows kept")
    try:
        imported = store.import_kit1(os.path.join(browser.HOME, "kit1"), log)
    except Exception as e:  # the old jobs never stop the engine from starting
        imported = 0
        log(f"kit 1.x jobs not imported, tried again on the next start: {type(e).__name__}: {e}")
    if imported:
        log(f"{imported} job(s) from kit 1.x imported with their results")
    worker.start()
    chrome = browser.find_chrome()
    where = f"http://localhost:{port}" if a.host == "127.0.0.1" else \
        f"{a.host}:{port} in this container, published on the host's 127.0.0.1"
    log(f"engine {VERSION} on {where} (browser: {chrome or 'none found'}{'' if headless else ', as a window'}"
        f"{', API key required' if env['SCRAPER_API_KEY'] else ''})")
    if not headless and browser.always_headless(chrome):
        log("SCRAPER_HEADLESS=0 is set, but the only browser is the downloaded headless shell, which cannot "
            "open a window: install Google Chrome (or Chromium, Edge, Brave) for window mode")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGBREAK"):  # Windows: scripts/engine.py stop sends Ctrl-Break to the engine's group
        signal.signal(signal.SIGBREAK, lambda *_: sys.exit(0))
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    code = 0
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"engine crashed: {type(e).__name__}: {e}")
        code = 1
    finally:
        worker.shutdown(wait=8)  # the job in progress saves its place and resumes on the next start
        log("engine stopped")
        # Website-pass threads may still be inside a fetch (up to SITE_HARD s each); the interpreter
        # would wait for them before closing the socket. The job is already saved: leave now.
        os._exit(code)


if __name__ == "__main__":
    main()
