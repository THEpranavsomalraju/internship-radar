#!/usr/bin/env python3
"""Internship radar: watch job lists and career sites, push new roles to ntfy.

Standard library only. Run from GitHub Actions every 5 minutes; see README.md.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")

DAY = 86400

US_STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
US_STATE_NAMES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
    "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]
STATE_NAME_RE = re.compile(r"\b(?:%s)\b" % "|".join(US_STATE_NAMES))
STATE_CODE_RE = re.compile(r",\s*([A-Z]{2})\b")
INTERN_RE = re.compile(r"\bintern(?:ship)?s?\b", re.I)
KEY_DROP_RE = re.compile(r"\b(?:summer|fall|autumn|winter|spring|20\d\d)\b")
COMPANY_SUFFIX_RE = re.compile(r"\b(?:inc|llc|ltd|corp|corporation|co)\b")


class SourceError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def now_ts():
    return int(time.time())


def short(msg, n=120):
    msg = " ".join(str(msg).split())
    return msg if len(msg) <= n else msg[: n - 3] + "..."


def clean(text):
    """Strip tags, unescape entities, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(html.unescape(text).split())


def http_get(url, ua, headers=None, timeout=30, tries=2):
    hdrs = {"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"}
    hdrs.update(headers or {})
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code != 429:
                break
            time.sleep(2 * (attempt + 1))
    if isinstance(last, urllib.error.HTTPError):
        raise SourceError("HTTP %d from %s" % (last.code, urllib.parse.urlsplit(url).netloc))
    raise SourceError("fetch failed: %s" % short(last, 80))


def git_head(repo, branch):
    try:
        out = subprocess.run(
            ["git", "ls-remote", repo, "refs/heads/" + branch],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise SourceError("git ls-remote timed out")
    sha = out.stdout.split()[0] if out.stdout.strip() else ""
    if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SourceError("git ls-remote failed: %s" % short(out.stderr or "no output", 80))
    return sha


def straighten(text):
    return text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')


def compile_blocklist(words):
    alts = "|".join(re.escape(w) for w in words)
    return re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % alts, re.I)


def location_is_us(loc, cfg):
    loc = loc.strip()
    if not loc:
        return False
    uf = cfg["us_filter"]
    tail = re.search(r",\s*([A-Z]{2})$", loc)
    if tail and tail.group(1) in US_STATE_CODES:
        return True  # "New London, CT" is not London
    for word in uf["blocked"]:
        if re.search(r"\b%s\b" % re.escape(word), loc):
            return False
    if loc.lower() == "remote":
        return True
    for phrase in uf["accept_phrases"]:
        if phrase != "Remote" and re.search(r"\b%s\b" % re.escape(phrase), loc):
            return True
    if re.match(r"US-", loc):
        return True
    if any(code in US_STATE_CODES for code in STATE_CODE_RE.findall(loc)):
        return True
    return bool(STATE_NAME_RE.search(loc))


def any_us(locations, cfg):
    return any(location_is_us(loc, cfg) for loc in locations)


def split_locations(loc):
    return [p for p in re.split(r"[;|]", loc or "") if p.strip()]


def posting(pid, company, title, location, url, posted=None):
    return {"id": str(pid), "company": company, "title": title,
            "location": location, "url": url, "posted": posted}


# ---------------------------------------------------------------- adapters
# Each adapter returns {"postings": [...], "found": int, "sha": str|None,
# "unchanged": bool, "full": bool}. postings are already filtered.

def adapt_simplify(src, cfg, ss, ctx):
    sha = git_head(src["repo"], src["branch"])
    if ss.get("seeded") and ss.get("sha") == sha:
        return {"unchanged": True, "sha": sha}
    try:
        data = json.loads(http_get(src["raw_url"], cfg["user_agent"], timeout=90))
    except ValueError as e:
        raise SourceError("bad JSON: %s" % short(e, 60))
    if not isinstance(data, list) or len(data) < 500 or "company_name" not in data[0]:
        raise SourceError("listings.json shape changed (%s rows)" % (len(data) if isinstance(data, list) else "?"))
    block = ctx["blocklist"]
    cats = set(src["categories"])
    out = []
    for row in data:
        if not (row.get("active") and row.get("is_visible")):
            continue
        if src["term"] not in (row.get("terms") or []):
            continue
        if (row.get("category") or "").lower() not in cats:
            continue
        degrees = row.get("degrees") or []
        if degrees and src["degree"] not in degrees:
            continue
        locs = row.get("locations") or []
        if not any_us(locs, cfg):
            continue
        title = row.get("title") or ""
        if block.search(straighten(title)):
            continue
        posted = None
        if row.get("date_posted"):
            posted = datetime.fromtimestamp(row["date_posted"], ctx["tz"]).strftime("%b %d")
        out.append(posting(row["id"], row.get("company_name", "?"), title,
                           "; ".join(locs), row.get("url", ""), posted))
    return {"postings": out, "found": len(data), "sha": sha, "full": True}


def adapt_speedyapply(src, cfg, ss, ctx):
    sha = git_head(src["repo"], src["branch"])
    if ss.get("seeded") and ss.get("sha") == sha:
        return {"unchanged": True, "sha": sha}
    text = http_get(src["raw_url"], cfg["user_agent"]).decode("utf-8", "replace")
    block = ctx["blocklist"]
    season = re.compile(r"\b(?:%s)\b" % "|".join(src["reject_title_words"]), re.I)
    out, found = [], 0
    for name in src["tables"]:
        m = re.search(r"<!--\s*%s_START\s*-->(.*?)<!--\s*%s_END\s*-->" % (name, name), text, re.S)
        if not m:
            raise SourceError("missing %s markers" % name)
        lines = [l.strip() for l in m.group(1).splitlines() if l.strip().startswith("|")]
        if len(lines) < 2:
            continue
        header = [c.strip().lower() for c in lines[0].strip("|").split("|")]
        try:
            ci, pi, li, ai = (header.index(k) for k in ("company", "position", "location", "posting"))
        except ValueError:
            raise SourceError("%s header changed: %s" % (name, short(lines[0], 60)))
        age_i = header.index("age") if "age" in header else None
        for line in lines[2:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < len(header):
                continue
            found += 1
            href = re.search(r'href="([^"]+)"', cells[ai])
            if not href:
                continue
            strong = re.search(r"<strong>(.*?)</strong>", cells[ci])
            company = clean(strong.group(1) if strong else cells[ci])
            title = clean(cells[pi])
            if season.search(title) or block.search(straighten(title)):
                continue
            age = cells[age_i] if age_i is not None else None
            out.append(posting(href.group(1), company, title, clean(cells[li]), href.group(1),
                               ("%s ago" % age) if age else None))
    if found == 0:
        raise SourceError("no table rows parsed")
    return {"postings": out, "found": found, "sha": sha, "full": True}


def keep_direct(p, cfg):
    return INTERN_RE.search(p["title"]) and any_us(split_locations(p["location"]), cfg)


def adapt_icims(src, cfg, ss, ctx):
    ua = cfg["user_agent"]
    raw, pages, page = [], 1, 0
    while page < pages and page < 30:
        url = src["url"] + ("&pr=%d" % page if page else "")
        body = http_get(url, ua).decode("utf-8", "replace")
        if 'id="searchForm"' not in body or "iCIMS_ListingsPage" not in body:
            raise SourceError("sanity marker missing (search form)")
        cards = re.findall(r'<li class="iCIMS_JobCardItem">(.*?)</li>', body, re.S)
        before = len(raw)
        for card in cards:
            link = re.search(r'href="([^"]*/jobs/(\d+)/[^"]*/job[^"]*)"', card)
            if not link:
                continue
            title = re.search(r"<h3[^>]*>(.*?)</h3>", card, re.S)
            loc = re.search(r"Job Locations</span>\s*<span[^>]*>(.*?)</span>", card, re.S)
            href = re.sub(r"[?&]in_iframe=1", "", html.unescape(link.group(1)))
            raw.append(posting(link.group(2), src["company"], clean(title.group(1) if title else ""),
                               clean(loc.group(1) if loc else ""), href))
        if cards and len(raw) == before:
            raise SourceError("job cards present but none parsed")
        nums = [int(n) for n in re.findall(r"[?&]pr=(\d+)", body)]
        pages = max(nums + [0]) + 1
        page += 1
    out = [p for p in raw if keep_direct(p, cfg)]
    return {"postings": out, "found": len(raw), "full": True}


def adapt_talentbrew(src, cfg, ss, ctx):
    ua = cfg["user_agent"]
    raw, page, total_pages = [], 1, 1
    while page <= total_pages and page <= 20:
        q = urllib.parse.urlencode({
            "ActiveFacetID": 0, "CurrentPage": page, "RecordsPerPage": 100, "Distance": 50,
            "RadiusUnitType": 0, "Keywords": src["keywords"], "Location": "", "ShowRadius": "False",
            "IsPagination": "True" if page > 1 else "False", "FacetType": 0,
            "SearchResultsModuleName": "Search Results", "SearchFiltersModuleName": "Search Filters",
            "SortCriteria": 0, "SortDirection": 0, "SearchType": 1, "ResultsType": 0,
        })
        body = http_get(src["base"] + "/search-jobs/results?" + q, ua,
                        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
        try:
            res = json.loads(body)["results"]
        except (ValueError, KeyError, TypeError):
            raise SourceError("results endpoint did not return JSON")
        total = re.search(r'data-total-results="(\d+)"', res)
        tp = re.search(r'data-total-pages="(\d+)"', res)
        if 'id="search-results"' not in res or not total:
            raise SourceError("sanity marker missing (search-results)")
        total_pages = int(tp.group(1)) if tp else 1
        items = re.findall(
            r'<a href="([^"]+)" data-job-id="(\d+)"[^>]*>\s*<h[23][^>]*>(.*?)</h[23]>(.*?)</a>', res, re.S)
        if int(total.group(1)) > 0 and not items:
            raise SourceError("%s results reported but none parsed" % total.group(1))
        for href, jid, title, rest in items:
            loc = re.search(r'class="job-location[^"]*"[^>]*>(.*?)</span>', rest, re.S)
            raw.append(posting(jid, src["company"], clean(title), clean(loc.group(1) if loc else ""),
                               urllib.parse.urljoin(src["base"], html.unescape(href))))
        page += 1
    out = [p for p in raw if keep_direct(p, cfg)]
    return {"postings": out, "found": len(raw), "full": True}


def avature_page(src, cfg, offset, by_date=True):
    url = "%s?%s" % (src["url"], urllib.parse.urlencode({
        "jobSort": src["sort_field"] if by_date else src["name_sort_field"],
        "jobSortDirection": "DESC" if by_date else "ASC",
        "jobRecordsPerPage": src["page_size"], "jobOffset": offset,
    }))
    body = http_get(url, cfg["user_agent"]).decode("utf-8", "replace")
    if 'id="tpt_jobSort"' not in body:
        raise SourceError("sanity marker missing (search form)")
    legend = re.search(r"\d+\s*-\s*\d+\s+of\s+(\d+)\s+jobs?", body)
    total = int(legend.group(1)) if legend else 0
    items = []
    for art in re.findall(r'<article class="article article--result">(.*?)</article>', body, re.S):
        link = re.search(r'<h3[^>]*>\s*<a href="([^"]*/JobDetail/[^"]*/(\d+))"[^>]*>(.*?)</a>', art, re.S)
        if not link:
            continue
        sub = re.search(r'article__header__text__subtitle">(.*?)</div>', art, re.S)
        spans = [clean(s) for s in re.findall(r"<span[^>]*>(.*?)</span>", sub.group(1) if sub else "", re.S)]
        posted = next((s[7:] for s in spans if s.startswith("Posted ")), None)
        loc = spans[0] if spans and not spans[0].startswith(("Req #", "Posted")) else ""
        items.append(posting(link.group(2), src["company"], clean(link.group(3)), loc,
                             html.unescape(link.group(1)), posted))
    if total and not items:
        raise SourceError("%d jobs reported but none parsed" % total)
    return items, total


def adapt_avature(src, cfg, ss, ctx):
    now = now_ts()
    full = not ss.get("seeded") or now - ss.get("last_full", 0) >= src["full_crawl_hours"] * 3600
    cutoff = (datetime.now(ctx["tz"]) - timedelta(days=src["recent_days"])).date()
    size = src["page_size"]
    items, total = avature_page(src, cfg, 0, by_date=not full)
    pages = [items]
    if full:
        # every page sorted by name (stable across requests), a few at a time
        offsets = list(range(size, min(total, 100 * size), size))
        with ThreadPoolExecutor(max_workers=4) as pool:
            pages.extend(r[0] for r in pool.map(lambda o: avature_page(src, cfg, o, False), offsets))
    else:
        # newest first: stop once a page reaches postings older than the cutoff
        offset = size
        while offset < total and len(pages) < src["max_incremental_pages"]:
            dates = []
            for p in pages[-1]:
                try:
                    dates.append(datetime.strptime(p["posted"] or "", "%d-%b-%Y").date())
                except ValueError:
                    pass
            if dates and min(dates) < cutoff:
                break
            pages.append(avature_page(src, cfg, offset)[0])
            offset += size
    raw, ids = [], set()
    for page in pages:
        for p in page:
            if p["id"] in ids:
                continue
            ids.add(p["id"])
            if p["posted"]:
                try:
                    p["posted"] = datetime.strptime(p["posted"], "%d-%b-%Y").strftime("%b %d")
                except ValueError:
                    pass
            raw.append(p)
    if full:
        ss["last_full"] = now
    out = [p for p in raw if keep_direct(p, cfg)]
    return {"postings": out, "found": len(raw), "full": full}


ADAPTERS = {
    "simplify": adapt_simplify,
    "speedyapply": adapt_speedyapply,
    "icims": adapt_icims,
    "talentbrew": adapt_talentbrew,
    "avature": adapt_avature,
}


# ---------------------------------------------------------------- push

def header_safe(value):
    value = unicodedata.normalize("NFKD", straighten(str(value)))
    value = value.encode("ascii", "ignore").decode("ascii")
    return " ".join(value.split())[:250]


def url_safe(url):
    return urllib.parse.quote(url or "", safe=":/?#[]@!$&'()*+,;=%~")


class Pusher:
    def __init__(self, cfg, state, topic, dry_run, tz):
        self.cfg, self.state, self.topic, self.dry_run = cfg, state, topic, dry_run
        today = datetime.now(tz).strftime("%Y-%m-%d")
        if state.get("push_day") != today:
            state["push_day"], state["push_count"] = today, 0
        self.sent = 0

    def send(self, title, body, click=None, priority="default", tags=None):
        if self.state["push_count"] >= self.cfg["daily_hard_cap"]:
            print("  [skip push, daily hard cap] %s" % title)
            return False
        headers = {"Title": header_safe(title), "Priority": priority}
        if tags:
            headers["Tags"] = header_safe(tags)
        if click:
            headers["Click"] = url_safe(click)
        if self.dry_run:
            print("  [dry-run push] %s | %s" % (headers["Title"], body.replace("\n", " / ")))
            return True
        if not self.topic:
            raise SystemExit("NTFY_TOPIC is not set")
        url = "%s/%s" % (self.cfg["ntfy_server"].rstrip("/"), self.topic)
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as e:
            print("  push failed: %s" % short(e, 80))
            return False
        self.state["push_count"] += 1
        self.sent += 1
        return True

    def send_roles(self, items):
        """items: list of (src, posting) already deduped."""
        if not items:
            return 0
        cfg = self.cfg
        if self.state["push_count"] >= cfg["daily_soft_cap"]:
            lines = ["%s: %s" % (p["company"], p["title"]) for _, p in items[:15]]
            if len(items) > 15:
                lines.append("+%d more" % (len(items) - 15))
            self.send("Radar digest: %d new roles" % len(items), "\n".join(lines),
                      click=items[0][0]["list_url"], tags="briefcase")
            return len(items)
        room = max(0, min(cfg["max_pushes_per_run"], cfg["daily_soft_cap"] - self.state["push_count"]))
        for src, p in items[:room]:
            direct = bool(src.get("direct"))
            lines = [p["location"] or "Location n/a", src["label"]]
            if p.get("posted"):
                lines.append("Posted " + p["posted"])
            self.send("%s: %s" % (p["company"], p["title"]), "\n".join(lines), click=p["url"],
                      priority="high" if direct else "default",
                      tags="rotating_light" if direct else "briefcase")
        rest = items[room:]
        if rest:
            by_src = {}
            for src, _ in rest:
                by_src.setdefault(src["name"], [src, 0])[1] += 1
            top = max(by_src.values(), key=lambda v: v[1])[0]
            body = "\n".join("%s: %d" % (s["label"], n) for s, n in by_src.values())
            self.send("Radar: %d more new roles" % len(rest), body, click=top["list_url"], tags="briefcase")
        return len(items)


# ---------------------------------------------------------------- state

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


def match_key(company, title):
    c = COMPANY_SUFFIX_RE.sub(" ", re.sub(r"[^a-z0-9 ]", " ", company.lower()))
    t = KEY_DROP_RE.sub(" ", re.sub(r"[^a-z0-9 ]", " ", title.lower()))
    return " ".join(c.split()) + "|" + " ".join(t.split())


def get_topic():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        path = os.path.join(HERE, ".ntfy_topic")
        if os.path.exists(path):
            topic = open(path).read().strip()
    return topic


# ---------------------------------------------------------------- main run

def run(args):
    cfg = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {})
    tz = ZoneInfo(cfg["timezone"])
    now = now_ts()
    state.setdefault("sources", {})
    state.setdefault("alerts", [])
    state.setdefault("runs", [])
    if "started" not in state:
        # first run ever: today's heartbeat would only report this run
        state["started"] = now
        state["last_heartbeat"] = datetime.now(tz).strftime("%Y-%m-%d")
    pusher = Pusher(cfg, state, get_topic(), args.dry_run, tz)
    ctx = {"tz": tz, "blocklist": compile_blocklist(cfg["title_blocklist"])}

    sources = cfg["sources"]
    if args.source:
        sources = [s for s in sources if s["name"] == args.source]
        if not sources:
            raise SystemExit("unknown source: %s (have: %s)" % (
                args.source, ", ".join(s["name"] for s in cfg["sources"])))
    full_run = not args.source
    if full_run:
        state["runs"].append(now)

    report, new_items = [], []
    for src in sources:
        ss = state["sources"].setdefault(src["name"], {})
        t0 = time.time()
        try:
            res = ADAPTERS[src["type"]](src, cfg, ss, ctx)
        except Exception as e:  # any adapter failure counts against the source
            err = short(e if isinstance(e, SourceError) else "%s: %s" % (type(e).__name__, e))
            ss["fail_count"] = ss.get("fail_count", 0) + 1
            ss["last_error"] = err
            report.append((src["label"], "-", "-", "FAIL: " + err))
            if ss["fail_count"] >= cfg["failure_threshold"] and \
                    now - ss.get("last_fail_push", 0) >= DAY:
                if pusher.send("Radar: %s failing: %s" % (src["label"], err),
                               "%d runs in a row. Last error: %s" % (ss["fail_count"], err),
                               click=src["list_url"], tags="warning"):
                    ss["last_fail_push"] = now
            continue

        if ss.get("last_fail_push"):
            pusher.send("Radar: %s recovered" % src["label"], "Source is working again.",
                        click=src["list_url"], tags="white_check_mark")
            ss.pop("last_fail_push", None)
        ss["fail_count"] = 0
        ss.pop("last_error", None)
        ss["last_ok"] = now
        secs = time.time() - t0

        if res.get("unchanged"):
            report.append((src["label"], "-", "-", "ok (no new commit, %.1fs)" % secs))
            continue

        postings = res["postings"]
        seen = ss.setdefault("seen", {})
        fresh = []
        for p in postings:
            if p["id"] not in seen:
                seen[p["id"]] = now
                fresh.append(p)
        if not ss.get("seeded"):
            ss["seeded"] = True
            pusher.send("Radar seeded: %s tracking %d postings" % (src["label"], len(postings)),
                        "Existing postings recorded silently. New ones will alert.",
                        click=src["list_url"], priority="low", tags="seedling")
            status = "ok (seeded %d)" % len(postings)
        else:
            new_items.extend((src, p) for p in fresh)
            status = "ok (%d new)" % len(fresh)
        if res.get("full"):
            keep = {p["id"] for p in postings}
            limit = now - cfg["seen_retention_days"] * DAY
            for pid in [k for k, ts in seen.items() if ts < limit and k not in keep]:
                del seen[pid]
        if res.get("sha"):
            ss["sha"] = res["sha"]
        report.append((src["label"], res["found"], len(postings), "%s, %.1fs" % (status, secs)))

        if args.dry_run and src.get("direct"):
            for p in postings:
                print("    %s | %s | %s" % (p["title"], p["location"], p["url"]))

    # cross-source dedupe; direct company watchers go first
    new_items.sort(key=lambda it: 0 if it[0].get("direct") else 1)
    dedupe_after = now - cfg["dedupe_days"] * DAY
    to_alert, silenced = [], 0
    for src, p in new_items:
        key = match_key(p["company"], p["title"])
        dup = any(a["key"] == key and a["ts"] >= dedupe_after and
                  (a["source"] != src["name"] or not src.get("direct"))
                  for a in state["alerts"])
        if dup:
            silenced += 1
            continue
        state["alerts"].append({"key": key, "source": src["name"], "ts": now})
        to_alert.append((src, p))
    pusher.send_roles(to_alert)

    if full_run:
        heartbeat(cfg, state, pusher, tz, now)

    state["runs"] = [t for t in state["runs"] if t >= now - DAY]
    state["alerts"] = [a for a in state["alerts"] if a["ts"] >= dedupe_after]
    state["last_run"] = now

    print("%-14s %8s %8s  %s" % ("source", "found", "passing", "status"))
    for row in report:
        print("%-14s %8s %8s  %s" % row)
    print("new roles: %d alerted, %d deduped | pushes this run: %d, today: %d" % (
        len(to_alert), silenced, pusher.sent, state["push_count"]))

    if not args.dry_run:
        save_state(state)


def heartbeat(cfg, state, pusher, tz, now):
    local = datetime.now(tz)
    today = local.strftime("%Y-%m-%d")
    if local.hour < cfg["heartbeat_hour"] or state.get("last_heartbeat") == today:
        return
    runs = len([t for t in state["runs"] if t >= now - DAY])
    alerted = len([a for a in state["alerts"] if a["ts"] >= now - DAY])
    lines = ["Runs (24h): %d of 288" % runs]
    for src in cfg["sources"]:
        ss = state["sources"].get(src["name"], {})
        if ss.get("fail_count"):
            lines.append("%s: failing x%d (%s)" % (src["label"], ss["fail_count"], ss.get("last_error", "")))
        else:
            lines.append("%s: ok" % src["label"])
    lines.append("Roles alerted (24h): %d" % alerted)
    slow = runs < cfg["heartbeat_slow_runs"] and now - state.get("started", 0) >= DAY
    title = "Radar heartbeat" + (": trigger slow" if slow else "")
    if pusher.send(title, "\n".join(lines), priority="low", tags="green_heart") or pusher.dry_run:
        state["last_heartbeat"] = today


def test_push(args):
    cfg = load_json(CONFIG_PATH, None)
    pusher = Pusher(cfg, {"push_day": "", "push_count": 0}, get_topic(), False, ZoneInfo(cfg["timezone"]))
    ok = pusher.send(
        "SAS: Summer 2027 - Software Development and Testing Intern",
        "US-NC-Cary HQ\nSAS\n(sample alert from radar --test-push)",
        click="https://careers-sas.icims.com/jobs/42964/summer-2027---software-development-and-testing-intern/job",
        priority="high", tags="rotating_light")
    print("test push sent" if ok else "test push FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Internship radar")
    ap.add_argument("--dry-run", action="store_true", help="print what would alert, change nothing")
    ap.add_argument("--test-push", action="store_true", help="send one sample push and exit")
    ap.add_argument("--source", help="run one source by name")
    args = ap.parse_args()
    if args.test_push:
        sys.exit(test_push(args))
    run(args)


if __name__ == "__main__":
    main()
