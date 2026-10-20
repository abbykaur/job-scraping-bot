import os
import re
import html
import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

import db
from datetime import datetime, timezone

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
# Feed 1 (security) reuses the original CHANNEL_ID; feed 2 (AI/ML) adds one;
# feeds 3 and 4 (Houston, Dallas AI/ML) add one each.
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHANNEL_ID_AIML = int(os.getenv("CHANNEL_ID_AIML", "0"))
CHANNEL_ID_HOUSTON_AIML = int(os.getenv("CHANNEL_ID_HOUSTON_AIML", "0"))
CHANNEL_ID_DALLAS_AIML = int(os.getenv("CHANNEL_ID_DALLAS_AIML", "0"))

# --- Configuration ---
LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
    ".github/scripts/listings.json"
)

# Categories the SimplifyJobs feed uses for AI/ML/Data roles (feed 2).
AIML_CATEGORIES = {
    "Data Science, AI & Machine Learning",
    "AI/ML/Data",
}

# JobSpy producer config: scrape real job boards for Bay Area security roles.
# Start minimal (Indeed only) to validate before adding scrape surface/block risk.
JOBSPY_SITES = ["indeed"]
JOBSPY_RESULTS_WANTED = 30
JOBSPY_HOURS_OLD = 168  # last 7 days

# Each JobSpy scrape is defined by a search term + location. The producer runs the
# scrape, then a caller-supplied keep(job) predicate filters the normalized results.
JOBSPY_SF_SECURITY = ("security engineer", "San Francisco, CA")
JOBSPY_HOUSTON_AIML = ("machine learning engineer", "Houston, TX")
JOBSPY_DALLAS_AIML = ("machine learning engineer", "Dallas, TX")

# The feed has no "Security" category, so we match security roles by scanning
# the job title for these keywords. Titles are lowercased before comparison.
#
# TODO(you): tune this set. It defines what counts as a "security job".
# Consider the full spectrum — offensive (red team, pentest, offensive security),
# defensive (SOC, incident response, detection), and governance (GRC, compliance),
# plus adjacent domains you care about (AI/LLM security, cloud security, appsec).
SECURITY_KEYWORDS = {
    # General
    "security", "cybersecurity", "infosec",
    # Offensive / red team
    "appsec", "penetration", "pentest", "red team", "offensive security",
    # Defensive / blue team
    "incident response", "threat", "vulnerability", "detection", "malware",
    "soc analyst", "blue team", "siem", "forensics",
    "threat intelligence", "detection engineer",
    # Crypto
    "cryptography",
    # NOTE: matched with \b word boundaries (see SECURITY_RE), so keep these
    # as whole words that appear in titles. Avoid short acronyms like "soc"/"iam"
    # unless you accept them matching standalone (they still won't hit "associate").
}

# Option A (light): compile the keyword set into ONE regex so each title is
# scanned in a single C-level pass instead of K separate Python-level substring
# checks. re.escape() each keyword so characters like "+" or "." stay literal.
#
# TODO(you): build the alternation pattern from SECURITY_KEYWORDS.
# Decide whether to anchor on word boundaries (\b...\b) to avoid substring
# collisions — e.g. "soc" matching "associate", "iam" matching "Williams".
# Word boundaries make matching stricter but can miss "AppSec" if you're not careful.
SECURITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in SECURITY_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Seniority signals — a title matching any of these is NOT a new-grad role.
# Used to filter general job-board results (JobSpy), which unlike SimplifyJobs
# are not inherently new-grad. Lenient by design: a plain "Security Engineer"
# with no level word is kept (many entry roles omit a level). Live comparison
# showed this cleanly drops Senior/Staff/Principal/Lead/III/Manager titles.
SENIORITY_RE = re.compile(
    r"\b(senior|sr|staff|principal|lead|manager|mgr|director|head|"
    r"ii|iii|iv|architect)\b",
    re.IGNORECASE,
)

# AI/ML title keywords — used to classify board-scraped jobs (JobSpy), which have
# no SimplifyJobs-style category. Word-boundary matched; kept tight to avoid false
# positives (e.g. "ml" only as a standalone token, not inside "html"/"xml").
AIML_TITLE_RE = re.compile(
    r"\b(machine learning|deep learning|artificial intelligence|"
    r"data scien(ce|tist)|ml engineer|ai engineer|mlops|"
    r"nlp|computer vision|ml|ai)\b",
    re.IGNORECASE,
)

# Physical Bay Area location keywords
BAY_AREA_KEYWORDS = {
    "san francisco", "bay area", "palo alto", "mountain view", "sunnyvale",
    "san jose", "menlo park", "santa clara", "redwood city", "oakland",
    "berkeley", "fremont", "cupertino", "south san francisco", "emeryville",
    "san mateo", "burlingame", "foster city", "milpitas",
}

# Companies based in or with massive hubs in the Bay Area to track for Remote roles
BAY_AREA_COMPANIES = {
    "apple", "google", "meta", "netflix", "uber", "lyft", "airbnb", "salesforce",
    "nvidia", "amd", "intel", "adobe", "roblox", "stripe", "openai", "anthropic",
    "linkedin", "pinterest", "zoom", "splunk", "twilio", "coinbase", "robinhood",
    "snowflake", "asana", "datadog", "instacart", "flexport", "cruise"
}

# Physical Greater Houston metro location keywords. Parallel to BAY_AREA_KEYWORDS
# — a second geography rather than a generalized region model (rule of three: we
# generalize when a 3rd city appears, not before). Unlike the Bay Area, we don't
# track a Houston remote-company list, so Houston matches on physical location only.
# Kept to distinctive metro city names; ambiguous ones that collide with cities in
# other states (e.g. "pasadena", "richmond", "spring") are omitted to avoid false
# positives, since matching is a plain substring test on the location string.
HOUSTON_KEYWORDS = {
    "houston", "sugar land", "the woodlands", "katy", "pearland", "conroe",
    "baytown", "league city", "missouri city", "humble", "kingwood", "tomball",
    "galveston", "texas city", "friendswood", "cypress", "spring, tx",
}

# Physical Dallas-Fort Worth metro location keywords, same approach as Houston.
# Matching is a plain substring test, so suburb names that collide with other
# places are pinned to ", tx": Arlington VA, Allentown PA, Addison IL, Carrollton GA,
# Odenton MD ("denton"), Irvington NJ ("irving"), Mesquite NV, Garland UT, etc.
DALLAS_KEYWORDS = {
    "dallas", "fort worth", "plano", "frisco", "mckinney", "coppell",
    "grand prairie", "southlake", "las colinas", "dfw",
    "irving, tx", "denton, tx", "garland, tx", "mesquite, tx", "lewisville, tx",
    "grapevine, tx", "arlington, tx", "allen, tx", "addison, tx",
    "carrollton, tx", "richardson, tx",
}

# Posted-state now lives in jobs.db (the `posted` column), replacing seen_jobs.json.
MAX_POSTS_PER_CYCLE = 10
# Candidates checked per feed per run. Higher than MAX_POSTS_PER_CYCLE so roles
# skipped by the experience check don't leave the channel short.
CANDIDATES_PER_FEED = 30

# Experience check. Titles rarely say "8+ years", so before posting we read the
# job description and skip roles whose MINIMUM required experience is above this.
# We take the minimum across mentions so "BS + 3 years OR MS + 1 year" reads as 1,
# and new-grad ranges like "0-2 years" pass. Postings we can't read are kept.
MAX_YEARS_REQUIRED = 2

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15,
}
_NUM = r"(\d{1,2}|" + "|".join(_NUM_WORDS) + r")"
# "5+ years", "3-5 years", "3 to 5 yrs", "five or more years". Plural only, so
# "4-year degree" and "80-year history" don't count.
YEARS_RE = re.compile(
    r"\b" + _NUM + r"\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*" + _NUM
    + r"\s*)?\+?\s*(?:or more\s+)?(?:years|yrs)\b",
    re.IGNORECASE,
)

# Workday pages render the description in the browser, so the raw HTML is empty.
# Its public JSON endpoint (/wday/cxs/...) returns the same text directly.
WORKDAY_RE = re.compile(
    r"https://([^/]+\.myworkday(?:jobs|site)\.com)/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"(?:recruiting/([^/]+)/)?([^/]+)/(job/[^?#]+)"
)

# Liveness check (hybrid, at post time only). HTTP statuses that mean the job
# posting is genuinely gone — everything else (incl. 3xx→2xx, 405, timeouts)
# is treated as alive (fail-open), since the feed already vouched for it.
DEAD_STATUSES = {404, 410}
LIVENESS_TIMEOUT = aiohttp.ClientTimeout(total=10)
# Some ATS sites 403 the default aiohttp UA; present a normal browser UA.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("csjobfeed")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


async def fetch_listings() -> list:
    """Raw fetch of the SimplifyJobs listings JSON (unnormalized source shape)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(LISTINGS_URL, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


def html_to_text(raw: str) -> str:
    """Flatten an HTML page (or HTML fragment) into searchable plain text.

    Keeps <script> blocks that mention "description", since many ATS pages
    (Greenhouse, Ashby) ship the posting as JSON inside a script tag.
    """
    raw = raw.replace("\\u003c", "<").replace("\\u003e", ">")
    raw = re.sub(
        r"<(script|style)[^>]*>(.*?)</\1>",
        lambda m: f" {m.group(2)} " if "description" in m.group(2).lower() else " ",
        raw, flags=re.S | re.I,
    )
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text)


async def fetch_posting(session: aiohttp.ClientSession, url: str) -> tuple[bool, str]:
    """GET a job posting once and return (alive, description_text).

    Doubles as the liveness check: alive is False only on a definitive 404/410.
    Fail-open like before: blocks, timeouts, and network errors return
    (True, ""), and empty text means "couldn't read it", which the experience
    check treats as a pass. We still don't detect 200-with-"position closed".
    """
    if not url:
        return True, ""  # nothing to check; trust the feed
    try:
        m = WORKDAY_RE.match(url)
        if m:
            host, tenant, site, path = m.groups()
            api = f"https://{host}/wday/cxs/{tenant or host.split('.')[0]}/{site}/{path}"
            async with session.get(api, headers={"Accept": "application/json"}) as resp:
                if resp.status in DEAD_STATUSES:
                    return False, ""
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    desc = data.get("jobPostingInfo", {}).get("jobDescription", "")
                    return True, html_to_text(desc)
            # Unexpected API response: fall back to the normal page below.
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status in DEAD_STATUSES:
                return False, ""
            if resp.status != 200:
                return True, ""
            return True, html_to_text(await resp.text(errors="ignore"))
    except Exception as e:
        log.debug("Fetch failed for %s (%s) — assuming alive.", url, e)
        return True, ""


def min_years_required(text: str) -> int | None:
    """Smallest years-of-experience number the posting asks for, or None if none.

    Only counts year mentions tied to "experience" (within 60 chars after or 30
    before), so "18 years of age" or "within 2 years of graduating" are ignored.
    """
    found = []
    for m in YEARS_RE.finditer(text):
        after = text[m.end():m.end() + 60].lower()
        before = text[max(0, m.start() - 30):m.start()].lower()
        if "experience" not in after and "experience" not in before:
            continue
        n = m.group(1).lower()
        found.append(int(n) if n.isdigit() else _NUM_WORDS[n])
    return min(found) if found else None


def too_experienced(text: str) -> int | None:
    """Return the required years if the posting asks for too much, else None."""
    years = min_years_required(text)
    if years is not None and years > MAX_YEARS_REQUIRED:
        return years
    return None


def normalize_simplify(raw: dict) -> dict:
    """Map one SimplifyJobs record into the db.jobs row shape, computing flags once.

    This is the source adapter: downstream code (feeds, embeds) only ever sees
    this normalized shape, never the raw source fields.
    """
    return {
        "job_id": job_id(raw),
        "source": "simplify",
        "company": raw.get("company_name", "Unknown Company"),
        "title": raw.get("title", "New Grad Role"),
        "url": raw.get("url", ""),
        "location": format_locations(raw),
        "category": raw.get("category", ""),
        "is_security": int(is_security_role(raw)),
        "is_aiml": int(is_aiml_role(raw)),
        "is_bay": int(is_bay_area(raw) or in_bay_area_scope(raw)),
        "is_houston": int(is_houston_area(raw)),
        "is_dallas": int(is_dallas_area(raw)),
        "scope": classify_scope(raw),
        "active": int(raw.get("active", True)),
        "date_posted": raw.get("date_posted", 0) or 0,
        "first_seen": datetime.now(timezone.utc).isoformat(),
    }


async def produce_simplify() -> int:
    """Producer: fetch SimplifyJobs, normalize, upsert, and prune vanished jobs.

    Returns the number of jobs ingested. Pruning hard-deletes any stored
    'simplify' job no longer present in this snapshot, so the DB reflects only
    currently-listed roles.
    """
    listings = await fetch_listings()
    normalized = [normalize_simplify(j) for j in listings]
    ingested = db.ingest(normalized)
    current_ids = [j["job_id"] for j in normalized]
    pruned = db.prune_missing("simplify", current_ids)
    if pruned:
        log.info("Pruned %d vanished job(s) from jobs.db.", pruned)
    return ingested


def _s(val) -> str:
    """Coerce a possibly-NaN/None pandas cell into a clean string."""
    if val is None:
        return ""
    s = str(val)
    return "" if s.lower() == "nan" else s.strip()


def normalize_jobspy(row) -> dict:
    """Map one JobSpy DataFrame row into the db.jobs row shape.

    Prefers job_url_direct (employer's ATS link — more durable) over job_url
    (Indeed redirect, which can expire — the "bad links" problem). Runs the
    regex security filter on the title since board search is fuzzy.
    """
    url = _s(row.get("job_url_direct")) or _s(row.get("job_url"))
    title = _s(row.get("title")) or "Security Role"
    company = _s(row.get("company")) or "Unknown Company"
    location = _s(row.get("location")) or "Not specified"
    # Reuse existing predicates by feeding them a dict shaped like they expect.
    as_job = {"title": title, "company_name": company, "location": location}
    is_remote_flag = bool(row.get("is_remote"))
    scope = classify_scope(as_job)
    return {
        "job_id": _s(row.get("id")) or f"jobspy|{company}|{title}|{url}",
        "source": "jobspy",
        "company": company,
        "title": title,
        "url": url,
        "location": location,
        "category": "",  # boards don't give SimplifyJobs-style categories
        "is_security": int(is_security_role(as_job)),
        "is_aiml": int(is_aiml_role(as_job)),
        "is_bay": int(is_bay_area(as_job) or (is_remote_flag and scope == "remote")),
        "is_houston": int(is_houston_area(as_job)),
        "is_dallas": int(is_dallas_area(as_job)),
        "scope": scope,
        "active": 1,
        "date_posted": 0,  # JobSpy gives a date, not the int epoch we sort by
        "first_seen": datetime.now(timezone.utc).isoformat(),
    }


def _scrape_jobspy(search_term: str, location: str):
    """Blocking JobSpy call, run in a thread by produce_jobspy."""
    from jobspy import scrape_jobs
    return scrape_jobs(
        site_name=JOBSPY_SITES,
        search_term=search_term,
        location=location,
        results_wanted=JOBSPY_RESULTS_WANTED,
        hours_old=JOBSPY_HOURS_OLD,
        country_indeed="USA",
    )


async def produce_jobspy(search, keep) -> int:
    """Generic JobSpy producer: scrape one (term, location), normalize, filter, upsert.

    `search` is a (search_term, location) tuple; `keep` is a predicate over a
    normalized job dict deciding what to ingest. JobSpy is synchronous, so it runs
    in a worker thread to avoid blocking the Discord event loop. We do NOT prune the
    'jobspy' source: a scrape returns only a sample (results_wanted), not a full
    snapshot, so absence doesn't mean a job is gone. Board search is fuzzy and not
    inherently new-grad, so `keep` must enforce role/location/new-grad.
    """
    search_term, location = search
    try:
        df = await asyncio.to_thread(_scrape_jobspy, search_term, location)
    except Exception as e:
        log.error("JobSpy scrape failed (%s @ %s): %s", search_term, location, e)
        return 0

    # Boards hand us the full description, so the experience check runs here at
    # ingest too. That covers redirect links (e.g. appcast) we can't read later.
    normalized = []
    for _, row in df.iterrows():
        job = normalize_jobspy(row)
        if keep(job) and not too_experienced(_s(row.get("description"))):
            normalized.append(job)
    if not normalized:
        return 0
    return db.ingest(normalized)


async def produce_jobspy_security() -> int:
    """SF security new-grad roles from job boards."""
    return await produce_jobspy(
        JOBSPY_SF_SECURITY,
        keep=lambda j: j["is_security"] and j["is_bay"] and is_new_grad(j),
    )


async def produce_jobspy_houston_aiml() -> int:
    """Houston AI/ML new-grad roles from job boards."""
    return await produce_jobspy(
        JOBSPY_HOUSTON_AIML,
        keep=lambda j: j["is_aiml"] and j["is_houston"] and is_new_grad(j),
    )


async def produce_jobspy_dallas_aiml() -> int:
    """Dallas-Fort Worth AI/ML new-grad roles from job boards."""
    return await produce_jobspy(
        JOBSPY_DALLAS_AIML,
        keep=lambda j: j["is_aiml"] and j["is_dallas"] and is_new_grad(j),
    )


def job_id(job: dict) -> str:
    return str(job.get("id") or f"{job.get('company_name')}|{job.get('title')}|{job.get('url')}")


def format_locations(job: dict) -> str:
    locs = job.get("locations") or []
    if isinstance(locs, list) and locs:
        return ", ".join(locs)
    return job.get("location", "Not specified")


def _location_matches(job: dict, keywords: set) -> bool:
    """True if any of the job's locations contains one of the given city keywords."""
    locs = job.get("locations") or job.get("location") or []
    if isinstance(locs, str):
        locs = [locs]
    for loc in locs:
        loc_lower = loc.lower()
        if any(keyword in loc_lower for keyword in keywords):
            return True
    return False


def is_bay_area(job: dict) -> bool:
    """Return True if any of the job's locations explicitly match a Bay Area city."""
    return _location_matches(job, BAY_AREA_KEYWORDS)


def is_houston_area(job: dict) -> bool:
    """Return True if any of the job's locations explicitly match a Houston-area city."""
    return _location_matches(job, HOUSTON_KEYWORDS)


def is_dallas_area(job: dict) -> bool:
    """Return True if any of the job's locations explicitly match a Dallas-Fort Worth city."""
    return _location_matches(job, DALLAS_KEYWORDS)


def is_remote(job: dict) -> bool:
    """Return True if the location explicitly mentions remote text."""
    locs = job.get("locations") or job.get("location") or []
    if isinstance(locs, str):
        locs = [locs]
    for loc in locs:
        if "remote" in loc.lower():
            return True
    return False


def is_security_role(job: dict) -> bool:
    """Return True if the job title matches any tracked security keyword."""
    return bool(SECURITY_RE.search(str(job.get("title", ""))))


def is_aiml_role(job: dict) -> bool:
    """Return True if the job is an AI/ML/Data role.

    Two signals: SimplifyJobs provides a structured `category` (authoritative when
    present); JobSpy provides only a free-text title, so we also match AI/ML title
    keywords. This keeps the predicate source-agnostic — feeds don't care where a
    job came from. Title terms are kept tight to avoid false positives.
    """
    if job.get("category", "") in AIML_CATEGORIES:
        return True
    return bool(AIML_TITLE_RE.search(str(job.get("title", ""))))


def is_new_grad(job: dict) -> bool:
    """Return True unless the title carries a seniority signal (Senior/Staff/etc).

    For general job-board sources (JobSpy) that aren't inherently new-grad.
    SimplifyJobs is already new-grad-only, so this isn't applied there.
    """
    return not SENIORITY_RE.search(str(job.get("title", "")))


def classify_scope(job: dict) -> str:
    """Classify a job's Bay Area scope exactly once.

    Returns "local" (physically in the Bay Area), "remote" (remote at a tracked
    Bay Area company), or "none" (not in scope). Callers stash this on the job so
    make_embed() doesn't recompute is_bay_area()/is_remote() all over again.
    """
    if is_bay_area(job):
        return "local"
    company_lower = str(job.get("company_name", "")).lower()
    if is_remote(job) and any(comp in company_lower for comp in BAY_AREA_COMPANIES):
        return "remote"
    return "none"


def in_bay_area_scope(job: dict) -> bool:
    """True if the job is physically in the Bay Area, or remote at a Bay Area company."""
    return classify_scope(job) != "none"


def matches_filters(job: dict) -> bool:
    """Keep security-focused roles that are also in the Bay Area scope."""
    # Cheap, high-rejection check first: skip anything that isn't a security role.
    if not is_security_role(job):
        return False
    return in_bay_area_scope(job)


# Each feed is a query over jobs.db (the "derive views" design). `where` is the
# SQL predicate the consumer ANDs onto "posted=0 AND active=1". Adding a feed
# later means adding an entry here — no schema change.
FEEDS = [
    {
        "name": "security",
        "channel_id": CHANNEL_ID,
        "where": "is_security=1 AND is_bay=1",
        "intro": "🔐 New Bay Area **security** new-grad roles:",
    },
    {
        "name": "aiml",
        "channel_id": CHANNEL_ID_AIML,
        "where": "is_aiml=1 AND is_bay=1",
        "intro": "🤖 New Bay Area **AI/ML** new-grad roles:",
    },
    {
        "name": "houston_aiml",
        "channel_id": CHANNEL_ID_HOUSTON_AIML,
        "where": "is_aiml=1 AND is_houston=1",
        "intro": "🤠 New Houston **AI/ML** new-grad roles:",
    },
    {
        "name": "dallas_aiml",
        "channel_id": CHANNEL_ID_DALLAS_AIML,
        "where": "is_aiml=1 AND is_dallas=1",
        "intro": "🐎 New Dallas-Fort Worth **AI/ML** new-grad roles:",
    },
]


def make_embed(row) -> discord.Embed:
    """Build a Discord embed from a normalized jobs.db row (sqlite3.Row/dict)."""
    company = row["company"] or "Unknown Company"
    title = row["title"] or "New Grad Role"
    url = row["url"] or ""
    scope = row["scope"] or "none"

    # Teal = physically in the Bay Area, blue = remote at a Bay Area company
    embed_color = discord.Color.teal() if scope == "local" else discord.Color.blue()

    embed = discord.Embed(
        title=f"{company} — {title}",
        url=url if url else None,
        color=embed_color,
    )

    embed.add_field(name="Location", value=row["location"] or "Not specified", inline=True)

    # Tag remote entries to explicitly highlight why they were matched
    if scope == "remote":
        embed.description = "📌 *Matched via Remote Bay Area Company tracking*"

    embed.set_footer(text=f"Source: {row['source']}")
    return embed


async def post_feed(feed: dict) -> int:
    """Consumer for one feed: query unposted matches, post them, mark posted."""
    if not feed["channel_id"]:
        log.warning("Feed '%s' has no channel configured; skipping.", feed["name"])
        return 0

    channel = bot.get_channel(feed["channel_id"])
    if channel is None:
        log.warning("Channel %s (feed '%s') not accessible yet.",
                    feed["channel_id"], feed["name"])
        return 0

    rows = db.fetch_unposted(feed["where"], limit=CANDIDATES_PER_FEED)
    if not rows:
        return 0

    posted_ids = []
    skipped = []  # (job_id, reason)
    async with aiohttp.ClientSession(
        timeout=LIVENESS_TIMEOUT, headers={"User-Agent": BROWSER_UA}
    ) as session:
        for row in rows:
            if len(posted_ids) >= MAX_POSTS_PER_CYCLE:
                break
            # One fetch: is the posting still live, and what does it ask for?
            alive, text = await fetch_posting(session, row["url"])
            if not alive:
                log.info("Dropping dead job %s (%s).", row["job_id"], row["url"])
                db.delete_job(row["job_id"])
                continue
            years = too_experienced(text)
            if years:
                log.info("Skipping %s — %s: asks for %d+ years.",
                         row["company"], row["title"], years)
                skipped.append((row["job_id"], f"experience:{years}"))
                continue
            try:
                await channel.send(embed=make_embed(row))
                posted_ids.append(row["job_id"])
                await asyncio.sleep(1)  # Gentle API spacing
            except discord.HTTPException as e:
                log.error("Failed to post job %s: %s", row["job_id"], e)

    db.mark_posted(posted_ids)
    db.mark_skipped(skipped)
    if skipped:
        log.info("Feed '%s': skipped %d role(s) for experience.", feed["name"], len(skipped))
    return len(posted_ids)


async def run_one_cycle():
    """One full pass: run all producers, then post each feed. Scheduling-agnostic."""
    # 1. Producers write normalized jobs into the DB. Each is isolated so one
    #    failing source (e.g. a JobSpy block) never stops the others.
    producers = (
        ("simplify", produce_simplify),
        ("jobspy-security", produce_jobspy_security),
        ("jobspy-houston-aiml", produce_jobspy_houston_aiml),
        ("jobspy-dallas-aiml", produce_jobspy_dallas_aiml),
    )
    for name, producer in producers:
        try:
            n = await producer()
            log.info("Producer '%s' ingested %d job(s).", name, n)
        except Exception as e:
            log.error("Producer '%s' failed: %s", name, e)

    # 2. Each feed is a consumer: query its subset, post, mark posted.
    for feed in FEEDS:
        posted = await post_feed(feed)
        log.info("Feed '%s': posted %d new job(s).", feed["name"], posted)


async def run_once():
    """Run-once entrypoint for scheduled (cron) execution.

    Logs in, waits until Discord is ready, runs a single cycle, then exits.
    Render Cron invokes this 3x/day on weekdays; there is no persistent process.
    """
    db.init_db()
    ready = asyncio.Event()

    @bot.event
    async def on_ready():
        log.info("Logged in as %s (run-once).", bot.user)
        ready.set()

    async with bot:
        login_task = asyncio.create_task(bot.start(TOKEN))
        try:
            await asyncio.wait_for(ready.wait(), timeout=60)
            await run_one_cycle()
        finally:
            await bot.close()
            # let the start task unwind
            try:
                await login_task
            except Exception:
                pass
    log.info("Run-once cycle complete; exiting.")


@bot.tree.command(name="jobs", description="Show recent new-grad roles from a feed")
@discord.app_commands.describe(
    feed="Which feed to show: security, aiml, houston_aiml, or dallas_aiml",
    count="How many to show (1-10)",
)
@discord.app_commands.choices(feed=[
    discord.app_commands.Choice(name="security", value="security"),
    discord.app_commands.Choice(name="aiml", value="aiml"),
    discord.app_commands.Choice(name="houston_aiml", value="houston_aiml"),
    discord.app_commands.Choice(name="dallas_aiml", value="dallas_aiml"),
])
async def jobs_command(
    interaction: discord.Interaction,
    feed: str = "security",
    count: int = 5,
):
    await interaction.response.defer()
    count = max(1, min(count, 10))  # Max 10 fits in one message block

    feed_cfg = next((f for f in FEEDS if f["name"] == feed), None)
    if feed_cfg is None:
        await interaction.followup.send(f"Unknown feed '{feed}'. Try 'security', 'aiml', 'houston_aiml', or 'dallas_aiml'.")
        return

    rows = db.fetch_recent(feed_cfg["where"], limit=count)
    if not rows:
        await interaction.followup.send(f"No active {feed} listings in the DB yet.")
        return

    embeds_to_send = [make_embed(row) for row in rows]
    await interaction.followup.send(
        f"Here are the {len(embeds_to_send)} most recent **{feed}** listings "
        "(Teal = Local, Blue = Remote):",
        embeds=embeds_to_send,
    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set in .env")
    if not CHANNEL_ID:
        raise SystemExit("CHANNEL_ID not set in .env (security feed channel)")
    if not CHANNEL_ID_AIML:
        log.warning("CHANNEL_ID_AIML not set — the AI/ML feed will be skipped.")
    if not CHANNEL_ID_HOUSTON_AIML:
        log.warning("CHANNEL_ID_HOUSTON_AIML not set — the Houston AI/ML feed will be skipped.")
    if not CHANNEL_ID_DALLAS_AIML:
        log.warning("CHANNEL_ID_DALLAS_AIML not set — the Dallas AI/ML feed will be skipped.")
    # Run-once mode: do one ingest+post cycle then exit. Scheduled 3x/day on
    # weekdays by Render Cron (see README / render.yaml). The /jobs slash command
    # is unavailable in this mode — there is no persistent process to serve it.
    asyncio.run(run_once())