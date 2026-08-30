"""
Fetches and parses LinkedIn profile data — no browser, plain HTTP
calls only.

The classic Voyager REST endpoint (voyager/api/identity/profiles/{id}/profileView)
returns 410 Gone; LinkedIn has moved profile pages to a newer,
proprietary "Server-Driven UI" system. The original REST-based client
(VoyagerClient) is kept below for reference in case that endpoint is
ever restored, but isn't used by default — see main.py, which uses
HTMLProfileClient instead.

HTMLProfileClient fetches nine things per profile:
- Topcard (name, headline, location, connections, photo) and experience
  are server-rendered directly into their pages' HTML — parsed with
  plain HTTP GETs.
- Education, skills, certifications, languages, and about are fetched
  client-side by LinkedIn's own frontend via POST calls to an internal
  action endpoint (flagship-web/rsc-action/actions/pagination or
  .../actions/component) — replicated here as plain HTTP POSTs with a
  JSON body built from a real captured request.

See the comment above each parse_*/build_*_payload function below for
how each specific section was found and how its response is parsed.
"""

import re
import html
from html.parser import HTMLParser

from .linkedin_session import LinkedInSession

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"


class _TopCardHTMLParser(HTMLParser):
    """
    Minimal dependency-free HTML parser (stdlib only, no BeautifulSoup
    needed) that walks the page looking for:
    - the first <title> tag (fallback name source)
    - the first <h2> tag (the profile name, in current markup)
    - <p> tag text within the same <section> as that h2 (topcard lines:
      headline, school, location, connections count, in DOM order)
    - the first profile photo <img src="...media.licdn.com...">

    FRAGILE BY NATURE: this walks structural tags (h2, p, section, img),
    not LinkedIn's obfuscated/hashed class names, which change on every
    deploy and are not a stable target. Structural tags are more durable
    but can still break if LinkedIn reorders the topcard's markup.
    """

    def __init__(self):
        super().__init__()
        self.in_title = False
        self.title_text = ""

        self.h2_found = False
        self.in_h2 = False
        self.name_text = ""

        self.capturing_topcard = False  # turns on right after </h2>
        self.in_p = False
        self.current_p_text = ""
        self.topcard_lines: list[str] = []

        self.profile_image = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "h2" and not self.h2_found:
            self.in_h2 = True
        elif tag == "p" and self.capturing_topcard and not self._topcard_capture_done():
            self.in_p = True
            self.current_p_text = ""
        elif tag == "img" and self.profile_image is None:
            src = attrs_dict.get("src", "")
            if "media.licdn.com" in src and ("profile" in src or "displayphoto" in src):
                self.profile_image = src

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "h2" and self.in_h2:
            self.in_h2 = False
            self.h2_found = True
            self.capturing_topcard = True
        elif tag == "p" and self.in_p:
            self.in_p = False
            text = self.current_p_text.strip()
            if text and text not in self.topcard_lines and not self._is_ui_noise(text):
                self.topcard_lines.append(text)
            if self._topcard_capture_done():
                self.capturing_topcard = False

    def handle_data(self, data):
        if self.in_title:
            self.title_text += data
        if self.in_h2 and not self.h2_found:
            self.name_text += data
        if self.in_p:
            self.current_p_text += data

    def _topcard_capture_done(self) -> bool:
        # Stop once we've hit a "connections" line (a reliable natural
        # end-of-topcard marker) or collected a generous cap of lines,
        # whichever comes first — avoids capturing unrelated page chrome.
        if any(re.search(r"connections?$", l, re.IGNORECASE) for l in self.topcard_lines):
            return True
        return len(self.topcard_lines) >= 8

    # Known LinkedIn UI prompts/labels that can appear as <p> tags between
    # the name and the real headline/school/location content — confirmed
    # live: "Verify in 2 minutes" (identity-verification banner) and
    # "Contact info" (a nav link) both got captured as if they were real
    # topcard fields before this filter was added. Not exhaustive — a
    # denylist heuristic, not a structural fix — so more may need adding
    # if other prompts surface on other profiles.
    _UI_NOISE_PATTERNS = (
        re.compile(r"^Verify\b.*\bminutes?$", re.IGNORECASE),
        re.compile(r"^Contact info$", re.IGNORECASE),
        re.compile(r"^Edit (profile|intro)$", re.IGNORECASE),
    )

    def _is_ui_noise(self, text: str) -> bool:
        return any(p.search(text) for p in self._UI_NOISE_PATTERNS)



def parse_top_card_from_html(html_text: str, profile_url: str) -> dict:
    # Get the name from <title> FIRST — it's reliable regardless of page
    # chrome (LinkedIn formats it as "Name | LinkedIn"). This matters
    # because blindly using the *first* <h2> in the whole document (the
    # old approach) breaks on a real full-page fetch: the actual page
    # includes LinkedIn's nav bar before the profile content, and that
    # nav bar has its own <h2>/accessibility text (e.g. a notification
    # bell labeled "0 notifications") that isn't the profile at all.
    # This bug never showed up against hand-copied dev snippets that
    # happened to start right at the profile content, only against a
    # real full-page response — confirmed live during testing.
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", html_text)
    title_name = title_match.group(1).split("|")[0].strip() if title_match else None

    # Find the profile photo across the WHOLE original page, independent
    # of the h2 slicing below. Confirmed live: slicing the HTML to start
    # at the real h2 (to fix the nav-h2 bug above) accidentally cut off
    # the photo too, since the <img> tag sits BEFORE the name in DOM
    # order (photo is positioned above/beside the name) — profile_image
    # came back None on a real page even though everything else worked.
    img_match = re.search(
        r'<img[^>]+src="([^"]*media\.licdn\.com[^"]*(?:profile|displayphoto)[^"]*)"',
        html_text,
    )
    # Unescape HTML entities (e.g. "&amp;" -> "&") — the raw src attribute
    # comes back HTML-encoded, confirmed live (query string had literal
    # "&amp;" instead of "&", which would break strict URL parsers).
    profile_image = html.unescape(img_match.group(1)) if img_match else None

    # Now locate the REAL topcard <h2> — the one whose text matches the
    # title-derived name — rather than trusting document order. Only
    # start the (name, headline, school, location, connections) capture
    # from that position onward, so nav/ad content earlier in the page
    # never gets mistaken for it.
    search_from = 0
    if title_name:
        h2_match = re.search(r"<h2[^>]*>\s*" + re.escape(title_name) + r"\s*</h2>", html_text)
        if h2_match:
            search_from = h2_match.start()

    parser = _TopCardHTMLParser()
    parser.feed(html_text[search_from:])

    name = title_name or (parser.name_text.strip() or None)

    # Drop lines that are just punctuation/symbols (e.g. a stray "·"
    # divider between UI elements) — confirmed live: one such line got
    # picked as "location" since it happened to sit right before the
    # connections count, the position the heuristic below trusts.
    lines = [l for l in parser.topcard_lines if re.search(r"[A-Za-z0-9]", l)]
    headline = lines[0] if lines else None
    connections = next((l for l in lines if re.search(r"connections?$", l, re.IGNORECASE)), None)
    # location heuristic: LinkedIn's topcard order is consistently
    # [headline, (school), location, connections] — so the line
    # immediately before the connections count is the most reliable
    # signal for location, rather than guessing by content shape.
    location = None
    if connections and connections in lines:
        idx = lines.index(connections)
        if idx > 0 and lines[idx - 1] != headline:
            location = lines[idx - 1]

    return {
        "url": profile_url,
        "name": name,
        "headline": headline,
        "location": location,
        "connections": connections,
        "about": None,
        "experience": [],
        "education": [],
        "skills": [],
        "certifications": [],
        "languages": [],
        "profile_image": profile_image,
    }


# --- Experience: fetched from the dedicated /details/experience/ page,
# which (unlike the main profile page's Experience section) IS
# server-rendered directly into plain HTML — no async loading needed.
#
# Each entry's "edit position" link appears twice: once wrapping the
# title/company/date/location block, and again later as a small
# standalone edit icon with no real content. Originally distinguished by
# a hashed CSS class prefix on the first occurrence — but LinkedIn's
# hashed classes rotate on redeploys (confirmed live: this broke between
# development and testing), so anchoring is now done on the stable href
# pattern alone. Since both occurrences share that href, each unique
# entry's TWO occurrences are both tried, keeping whichever one actually
# yields a title — the content-wrapping one — rather than assuming which
# comes first in document order.
ANCHOR_HREF_RE = re.compile(
    r'href="https://www\.linkedin\.com/in/[^"]+/details/experience/edit/forms/(\d+)/"'
)


def _extract_experience_fields(html_text: str, start: int, pos_id: str) -> dict | None:
    end_marker = f'_position_{pos_id}_edit"'
    end = html_text.find(end_marker, start)
    if end == -1 or end - start > 4000:
        end = start + 1500  # this occurrence isn't the content-wrapping one; keep window small
    block = html_text[start:end]

    p_texts = re.findall(r'<p[^>]*>([^<]+)</p>', block)
    if not p_texts:
        return None  # this was the icon-only occurrence, not the content one

    title = p_texts[0] if len(p_texts) > 0 else None
    company = p_texts[1] if len(p_texts) > 1 else None
    date_range = p_texts[2] if len(p_texts) > 2 else None
    # location is only present for some entries (e.g. not shown for
    # "Self-employed"), so this may legitimately be None.
    location = p_texts[3] if len(p_texts) > 3 else None

    desc_match = re.search(r'data-testid="expandable-text-box"[^>]*>(.*?)</span>', block, re.DOTALL)
    description = None
    if desc_match:
        raw = re.sub(r'<br\s*/?>', '\n', desc_match.group(1))
        raw = re.sub(r'<[^>]+>', '', raw)
        description = raw.strip()

    skills_match = re.search(r'skill-associations-details/">.*?</span>\s*([^<]+)</a>', block, re.DOTALL)
    skills_snippet = skills_match.group(1).strip() if skills_match else None

    return {
        "title": title,
        "company": company,
        "date_range": date_range,
        "location": location,
        "description": description,
        "skills_snippet": skills_snippet,
    }


def parse_experience_from_details_html(html_text: str) -> list[dict]:
    # Only the portion before the hydration script is plain server-rendered
    # HTML; everything after it is escaped JSON we don't need and don't
    # want the regexes below accidentally matching against.
    cutoff = html_text.find('id="rehydrate-data"')
    if cutoff != -1:
        html_text = html_text[:cutoff]

    occurrences_by_id: dict[str, list[int]] = {}
    for m in ANCHOR_HREF_RE.finditer(html_text):
        occurrences_by_id.setdefault(m.group(1), []).append(m.end())

    experiences = []
    for pos_id, positions in occurrences_by_id.items():
        entry = None
        for start in positions:
            entry = _extract_experience_fields(html_text, start, pos_id)
            if entry is not None:
                break
        if entry is not None:
            experiences.append(entry)
    return experiences


# --- Education: fetched via the "pagination" RSC-action endpoint.
#
# Unlike experience, education's DOM section on its own /details/education/
# page is a genuine client-side-only placeholder (a loading spinner) — the
# real data is NOT in that page's HTML at all, confirmed by exhaustive
# testing (see README "Approach" / "Known limitations" for the full
# investigation trail). What IS in that page's HTML is a componentkey
# string that embeds the profile's internal LinkedIn ID
# (e.g. "com.linkedin.sdui.profile.card.refACoAAFt...EducationDetailsSection").
#
# That ID is the key needed to call a separate endpoint LinkedIn's own
# frontend calls to populate the section:
#   POST /flagship-web/rsc-action/actions/pagination?sduiid=com.linkedin.sdui.pagers.profile.details.education
# discovered by capturing the real browser request (Network tab) after
# clearing local/IndexedDB storage to force a fresh fetch, since a stale
# client-side cache had been masking it in earlier capture attempts. The
# request body shape below is copied from that real captured request.
#
# The response is in the same numbered-chunk RSC-stream format used
# throughout this app's HTML captures (e.g. `12:["$","p",null,{...,
# "children":["Ramaiah Institute Of Technology"]}]`), just delivered as a
# POST response body instead of embedded in a GET'd page.
PROFILE_ID_RE = re.compile(r'profile\.card\.ref([A-Za-z0-9_\-]+)EducationDetailsSection')
SCHOOL_TEXT_RE = re.compile(r'"children":\["([^"]+)"\]')


def extract_profile_id_from_html(html_text: str) -> str | None:
    m = PROFILE_ID_RE.search(html_text)
    return m.group(1) if m else None


def build_education_pagination_payload(vanity_name: str, profile_id: str) -> dict:
    component_ref = f"com.linkedin.sdui.profile.card.ref{profile_id}EducationDetailsSection"
    shared_payload = {
        "vanityName": vanity_name,
        "profileId": profile_id,
        "start": 0,
        "count": 10,  # only the first 10 entries — see README limitations
        "detailSectionReplaceableComponentRef": component_ref,
    }
    return {
        "pagerId": "com.linkedin.sdui.pagers.profile.details.education",
        "clientArguments": {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": shared_payload,
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "states": [],
            "screenId": "com.linkedin.sdui.flagshipnav.profile.ProfileEducationDetails",
            "knownTemplateIds": [],
        },
        "paginationRequest": {
            "$type": "proto.sdui.actions.requests.PaginationRequest",
            "pagerId": "com.linkedin.sdui.pagers.profile.details.education",
            "trigger": {
                "$case": "itemDistanceTrigger",
                "itemDistanceTrigger": {
                    "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                    "preloadDistance": 3,
                    "preloadLength": 250,
                },
            },
            "retryCount": 2,
            "requestedArguments": {
                "$type": "proto.sdui.actions.requests.RequestedArguments",
                "requestedStateKeys": [],
                "payload": shared_payload,
                "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            },
        },
    }


def parse_education_from_pagination_response(response_text: str) -> list[dict]:
    """
    Parses the RSC-stream response body from the education pagination
    endpoint. Grades appear as a separate early batch in the same order
    as the entries they belong to (confirmed against a real two-entry
    response); other fields (degree, dates, description) are grouped
    per-entity by scanning forward from each school name's real content
    position to the next one.
    """
    grades = re.findall(r'"children":\["(Grade: [^"]+)"\]', response_text)

    school_positions = []
    for m in SCHOOL_TEXT_RE.finditer(response_text):
        val = m.group(1)
        low = val.lower()
        if val == "Education":
            continue
        if any(w in low for w in ["institute", "university", "college", "school of"]):
            school_positions.append((m.start(), val))

    entries = []
    for i, (pos, school) in enumerate(school_positions):
        end = school_positions[i + 1][0] if i + 1 < len(school_positions) else len(response_text)
        window = response_text[pos:end]
        fields = [f for f in SCHOOL_TEXT_RE.findall(window) if f != school]

        degree = fields[0] if len(fields) > 0 else None
        date_range = fields[1] if len(fields) > 1 else None
        description = fields[2] if len(fields) > 2 and not fields[2].startswith("Grade") else None

        entries.append({
            "school": school,
            "degree": degree,
            "date_range": date_range,
            "grade": grades[i] if i < len(grades) else None,
            "description": description,
        })
    return entries


# --- Skills: same pagination-endpoint mechanism as education, different
# sduiid ("com.linkedin.sdui.pagers.profile.details.skills"). Confirmed
# by capturing a real request the same way education's was found.
#
# The response pairs each skill with an adjacent "context" string (e.g.
# a certification or position it's associated with), but WHICH of each
# pair is the real skill name is not reliably determined by position —
# unlike education, the order flips partway through the real captured
# response (skill-then-context for some entries, context-then-skill for
# others). Rather than guess, this uses a distinct, unambiguous anchor
# also present in the same response: each skill has an
# "aria-label":"Edit <NAME> skill" string next to it. Only the skill
# name is extracted this way — the associated-context pairing is left
# out rather than risk mislabeling it.
def build_skills_pagination_payload(vanity_name: str, profile_id: str) -> dict:
    component_ref = f"com.linkedin.sdui.profile.card.ref{profile_id}SkillsDetailsSection"
    shared_payload = {
        "vanityName": vanity_name,
        "profileId": profile_id,
        "start": 0,
        "count": 25,
        "detailSectionReplaceableComponentRef": component_ref,
    }
    return {
        "pagerId": "com.linkedin.sdui.pagers.profile.details.skills",
        "clientArguments": {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": shared_payload,
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "states": [],
            "screenId": "com.linkedin.sdui.flagshipnav.profile.ProfileSkillsDetails",
            "knownTemplateIds": [],
        },
        "paginationRequest": {
            "$type": "proto.sdui.actions.requests.PaginationRequest",
            "pagerId": "com.linkedin.sdui.pagers.profile.details.skills",
            "trigger": {
                "$case": "itemDistanceTrigger",
                "itemDistanceTrigger": {
                    "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                    "preloadDistance": 3,
                    "preloadLength": 250,
                },
            },
            "retryCount": 2,
            "requestedArguments": {
                "$type": "proto.sdui.actions.requests.RequestedArguments",
                "requestedStateKeys": [],
                "payload": shared_payload,
                "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            },
        },
    }


SKILL_NAME_RE = re.compile(r'"aria-label":"Edit ([^"]+) skill"')


def parse_skills_from_pagination_response(response_text: str) -> list[str]:
    return SKILL_NAME_RE.findall(response_text)


# --- Certifications: same pagination endpoint pattern as education/skills,
# sduiid "...pagers.profile.details.certifications", confirmed against a
# real captured response (10 entries, all correct). Uses the same
# anchor-based grouping technique as education (locate each entry's real
# name-content position via a stable text marker, then scan forward to
# the next entry's start), since the field order per entry (issuer,
# "Issued <date>", optional "Credential ID <id>") was consistent across
# all 10 real entries tested.
def build_certifications_pagination_payload(vanity_name: str, profile_id: str) -> dict:
    component_ref = f"com.linkedin.sdui.profile.card.ref{profile_id}CertificationsDetailsSection"
    shared_payload = {
        "vanityName": vanity_name,
        "profileId": profile_id,
        "start": 0,
        "count": 25,
        "detailSectionReplaceableComponentRef": component_ref,
    }
    return {
        "pagerId": "com.linkedin.sdui.pagers.profile.details.certifications",
        "clientArguments": {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": shared_payload,
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "states": [],
            "screenId": "com.linkedin.sdui.flagshipnav.profile.ProfileCertificationsDetails",
            "knownTemplateIds": [],
        },
        "paginationRequest": {
            "$type": "proto.sdui.actions.requests.PaginationRequest",
            "pagerId": "com.linkedin.sdui.pagers.profile.details.certifications",
            "trigger": {
                "$case": "itemDistanceTrigger",
                "itemDistanceTrigger": {
                    "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                    "preloadDistance": 3,
                    "preloadLength": 250,
                },
            },
            "retryCount": 2,
            "requestedArguments": {
                "$type": "proto.sdui.actions.requests.RequestedArguments",
                "requestedStateKeys": [],
                "payload": shared_payload,
                "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            },
        },
    }


CERT_ANCHOR_RE = re.compile(r'aria-label":"Edit license or certification ([^"]+)"')
CHILD_TEXT_RE = re.compile(r'"children":\["([^"]+)"\]')


def parse_certifications_from_pagination_response(response_text: str) -> list[dict]:
    anchor_names = [m.group(1) for m in CERT_ANCHOR_RE.finditer(response_text)]

    name_positions = []
    for name in anchor_names:
        m = re.search(re.escape('"children":["' + name), response_text)
        if m:
            name_positions.append((m.start(), name))

    entries = []
    for i, (pos, name) in enumerate(name_positions):
        end = name_positions[i + 1][0] if i + 1 < len(name_positions) else len(response_text)
        window = response_text[pos:end]
        fields = [f for f in CHILD_TEXT_RE.findall(window) if f != name]
        issuer = fields[0] if len(fields) > 0 else None
        issued = fields[1] if len(fields) > 1 and fields[1].startswith("Issued") else None
        credential_id = next((f for f in fields if f.startswith("Credential ID")), None)
        entries.append({"name": name, "issuer": issuer, "issued": issued, "credential_id": credential_id})
    return entries


# --- Languages: same pagination pattern, sduiid "...pagers.profile.details.languages".
# Simplest response of all — clean, regular (language, proficiency) pairs
# with no ambiguity, confirmed against a real 4-language response.
def build_languages_pagination_payload(vanity_name: str, profile_id: str) -> dict:
    component_ref = f"com.linkedin.sdui.profile.card.ref{profile_id}LanguagesDetailsSection"
    shared_payload = {
        "vanityName": vanity_name,
        "profileId": profile_id,
        "start": 0,
        "count": 25,
        "detailSectionReplaceableComponentRef": component_ref,
    }
    return {
        "pagerId": "com.linkedin.sdui.pagers.profile.details.languages",
        "clientArguments": {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": shared_payload,
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "states": [],
            "screenId": "com.linkedin.sdui.flagshipnav.profile.ProfileLanguagesDetails",
            "knownTemplateIds": [],
        },
        "paginationRequest": {
            "$type": "proto.sdui.actions.requests.PaginationRequest",
            "pagerId": "com.linkedin.sdui.pagers.profile.details.languages",
            "trigger": {
                "$case": "itemDistanceTrigger",
                "itemDistanceTrigger": {
                    "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                    "preloadDistance": 3,
                    "preloadLength": 250,
                },
            },
            "retryCount": 2,
            "requestedArguments": {
                "$type": "proto.sdui.actions.requests.RequestedArguments",
                "requestedStateKeys": [],
                "payload": shared_payload,
                "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            },
        },
    }


def parse_languages_from_pagination_response(response_text: str) -> list[dict]:
    fields = CHILD_TEXT_RE.findall(response_text)
    fields = [f for f in fields if f != "Languages"]
    languages = []
    for i in range(0, len(fields) - 1, 2):
        languages.append({"name": fields[i], "proficiency": fields[i + 1]})
    return languages


# --- About: fetched via a different action endpoint from the others —
# /flagship-web/rsc-action/actions/component (singular), not
# .../actions/pagination, since About is one block of text, not a
# paginated list. The componentId/sduiid here are fixed, generic values
# ("...profileCardsAboveActivity") rather than embedding the profile's
# internal ID — differentiation happens via vanityName in the body
# instead.
#
# The response needed a different parser than the other sections:
# About's text comes back as several separate rich-text segments (one
# per paragraph/line break) referenced through a chain of "$L<id>"
# placeholders, not a single plain string. resolve_about_text below
# walks that reference chain generically (not hardcoded chunk numbers,
# since those differ per response) rather than guessing a fixed
# position.
def build_about_component_payload(vanity_name: str, profile_id: str | None = None) -> dict:
    payload = {
        "isSelfView": False,
        "vanityName": vanity_name,
    }
    if profile_id:
        payload["profileId"] = profile_id
    return {
        "clientArguments": {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": payload,
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "states": [],
        }
    }


def _get_rsc_chunk(chunk_id: str, full_text: str) -> str | None:
    m = re.search(r'(?:^|\n)' + re.escape(chunk_id) + r':(.*?)(?=\n[0-9a-fA-F]+:|\Z)', full_text, re.DOTALL)
    return m.group(1) if m else None


def resolve_about_text(response_text: str) -> str | None:
    m = re.search(r'componentKey":"[^"]*About"[^}]*"initialContent":"\$L([0-9a-fA-F]+)"', response_text)
    if not m:
        return None
    outer_chunk = _get_rsc_chunk(m.group(1), response_text)
    if not outer_chunk:
        return None

    candidate_ids = re.findall(r'\$L([0-9a-fA-F]+)', outer_chunk)
    for cid in candidate_ids:
        chunk = _get_rsc_chunk(cid, response_text)
        if not chunk:
            continue
        segments = re.findall(r'"children":\[(?:null|\[[^\]]*\]),\s*"((?:[^"\\]|\\.)*)"\]', chunk)
        total_len = sum(len(s) for s in segments)
        if len(segments) >= 2 and total_len > 50:
            return "\n".join(s for s in segments if s.strip())
    return None


class HTMLProfileClient:
    """Fetches the profile page (topcard), the /details/experience/ page
    (full experience list, server-rendered), and calls the education
    pagination action endpoint (education is NOT server-rendered, so it
    needs this separate POST call) — merges all three into one response."""

    def __init__(self, session: LinkedInSession):
        self.session = session

    def fetch_profile(self, public_identifier: str, profile_url: str) -> dict:
        url = f"https://www.linkedin.com/in/{public_identifier}/"
        html_text = self.session.get_html(url)
        data = parse_top_card_from_html(html_text, profile_url)

        try:
            exp_url = f"https://www.linkedin.com/in/{public_identifier}/details/experience/"
            exp_html = self.session.get_html(exp_url)
            data["experience"] = parse_experience_from_details_html(exp_html)
        except Exception as e:
            # Don't let a failure here take down the whole profile response —
            # topcard data is still useful even if experience fetch fails.
            data["_experience_fetch_error"] = str(e)

        profile_id = None
        try:
            edu_page_url = f"https://www.linkedin.com/in/{public_identifier}/details/education/"
            edu_html = self.session.get_html(edu_page_url)
            profile_id = extract_profile_id_from_html(edu_html)
            if not profile_id:
                raise ValueError("Could not extract internal profile ID from education page HTML.")
            payload = build_education_pagination_payload(public_identifier, profile_id)
            pagination_url = (
                "https://www.linkedin.com/flagship-web/rsc-action/actions/pagination"
                "?sduiid=com.linkedin.sdui.pagers.profile.details.education"
            )
            response_text = self.session.post_json(pagination_url, payload, referer=edu_page_url)
            data["education"] = parse_education_from_pagination_response(response_text)
        except Exception as e:
            data["_education_fetch_error"] = str(e)

        try:
            # profile_id is the same across all of a profile's /details/
            # pages — reuse it from education above rather than fetching
            # /details/skills/ HTML again just to extract it a second time.
            if not profile_id:
                skills_page_url = f"https://www.linkedin.com/in/{public_identifier}/details/skills/"
                skills_html = self.session.get_html(skills_page_url)
                profile_id = extract_profile_id_from_html(skills_html)
            if not profile_id:
                raise ValueError("Could not determine internal profile ID for skills fetch.")

            skills_payload = build_skills_pagination_payload(public_identifier, profile_id)
            skills_pagination_url = (
                "https://www.linkedin.com/flagship-web/rsc-action/actions/pagination"
                "?sduiid=com.linkedin.sdui.pagers.profile.details.skills"
            )
            skills_referer = f"https://www.linkedin.com/in/{public_identifier}/details/skills/"
            skills_response_text = self.session.post_json(skills_pagination_url, skills_payload, referer=skills_referer)
            data["skills"] = parse_skills_from_pagination_response(skills_response_text)
        except Exception as e:
            data["_skills_fetch_error"] = str(e)

        try:
            if not profile_id:
                raise ValueError("No profile ID available (earlier fetches failed) — cannot fetch certifications.")
            cert_payload = build_certifications_pagination_payload(public_identifier, profile_id)
            cert_pagination_url = (
                "https://www.linkedin.com/flagship-web/rsc-action/actions/pagination"
                "?sduiid=com.linkedin.sdui.pagers.profile.details.certifications"
            )
            cert_referer = f"https://www.linkedin.com/in/{public_identifier}/details/certifications/"
            cert_response_text = self.session.post_json(cert_pagination_url, cert_payload, referer=cert_referer)
            data["certifications"] = parse_certifications_from_pagination_response(cert_response_text)
        except Exception as e:
            data["_certifications_fetch_error"] = str(e)

        try:
            if not profile_id:
                raise ValueError("No profile ID available (earlier fetches failed) — cannot fetch languages.")
            lang_payload = build_languages_pagination_payload(public_identifier, profile_id)
            lang_pagination_url = (
                "https://www.linkedin.com/flagship-web/rsc-action/actions/pagination"
                "?sduiid=com.linkedin.sdui.pagers.profile.details.languages"
            )
            lang_referer = f"https://www.linkedin.com/in/{public_identifier}/details/languages/"
            lang_response_text = self.session.post_json(lang_pagination_url, lang_payload, referer=lang_referer)
            data["languages"] = parse_languages_from_pagination_response(lang_response_text)
        except Exception as e:
            data["_languages_fetch_error"] = str(e)

        try:
            about_payload = build_about_component_payload(public_identifier, profile_id)
            about_url = (
                "https://www.linkedin.com/flagship-web/rsc-action/actions/component"
                "?componentId=com.linkedin.sdui.generated.profile.dsl.impl.profileCardsAboveActivity"
                "&sduiid=com.linkedin.sdui.generated.profile.dsl.impl.profileCardsAboveActivity"
            )
            about_referer = f"https://www.linkedin.com/in/{public_identifier}/"
            about_response_text = self.session.post_json(about_url, about_payload, referer=about_referer)
            data["about"] = resolve_about_text(about_response_text)
        except Exception as e:
            data["_about_fetch_error"] = str(e)

        return data




# --- Deprecated: original REST-based client, kept for reference ---
# The profileView endpoint below returns 410 Gone as of this project's
# development. Left in place in case it's restored for some accounts,
# or as a starting point if you replicate the newer SDUI protocol.

class VoyagerClient:
    def __init__(self, session: LinkedInSession):
        self.session = session

    def fetch_profile_view(self, public_identifier: str) -> dict:
        url = f"{VOYAGER_BASE}/identity/profiles/{public_identifier}/profileView"
        return self.session.get(url)

    def fetch_skills(self, public_identifier: str) -> dict:
        url = f"{VOYAGER_BASE}/identity/profiles/{public_identifier}/skillCategory"
        try:
            return self.session.get(url)
        except Exception:
            return {}


def _index_included(raw: dict) -> dict:
    return {item.get("entityUrn"): item for item in raw.get("included", []) if item.get("entityUrn")}


def _date_range(entity: dict) -> str | None:
    tr = entity.get("timePeriod") or {}
    start = tr.get("startDate") or {}
    end = tr.get("endDate")
    if not start:
        return None
    s = f"{start.get('month', '?')}/{start.get('year', '?')}"
    e = "Present" if end is None else f"{end.get('month', '?')}/{end.get('year', '?')}"
    return f"{s} - {e}"


def parse_profile(profile_view_raw: dict, skills_raw: dict, profile_url: str) -> dict:
    """Kept for reference — parses the old REST profileView shape. Not
    used by default since that endpoint now returns 410 Gone."""
    index = _index_included(profile_view_raw)
    profile_entity = next(
        (v for v in index.values() if "Profile" in v.get("$type", "") and "positionGroups" not in v),
        profile_view_raw.get("data", {}),
    )

    experiences = []
    for item in index.values():
        if "Position" in item.get("$type", ""):
            experiences.append({
                "title": item.get("title"),
                "company": item.get("companyName"),
                "date_range": _date_range(item),
                "description": item.get("description"),
                "location": item.get("locationName"),
            })

    education = []
    for item in index.values():
        if "Education" in item.get("$type", ""):
            education.append({
                "school": item.get("schoolName"),
                "degree": item.get("degreeName"),
                "field_of_study": item.get("fieldOfStudy"),
                "date_range": _date_range(item),
            })

    skills = []
    for item in (skills_raw.get("included") or []):
        name = item.get("name")
        if name and "Skill" in item.get("$type", ""):
            skills.append(name)

    return {
        "url": profile_url,
        "name": " ".join(filter(None, [profile_entity.get("firstName"), profile_entity.get("lastName")])) or None,
        "headline": profile_entity.get("headline"),
        "location": (profile_entity.get("geoLocationName") or profile_entity.get("locationName")),
        "about": profile_entity.get("summary"),
        "experience": experiences,
        "education": education,
        "skills": skills,
        "certifications": [],
        "languages": [],
        "profile_image": None,
    }
