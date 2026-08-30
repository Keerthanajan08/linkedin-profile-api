"""
Holds the LinkedIn session cookies / CSRF token needed to call the
internal Voyager API.

Three ways to establish a session, tried in this order:

1. FULL COOKIE STRING (most reliable — use this one): if
   LINKEDIN_COOKIE_STRING is set, every cookie in it is loaded as-is.
   LinkedIn's session validation checks more than just li_at and
   JSESSIONID — it also checks device/browser-identifying cookies like
   bcookie, bscookie, and lidc. Sending only two of the cookies a real
   browser would send can read as an inconsistent/untrusted client and
   cause LinkedIn to defensively invalidate the whole session (which is
   also why the *browser tab* can get logged out when this happens).
   Get this from DevTools → Network tab: click any request to
   linkedin.com → Headers → find "Cookie:" under Request Headers →
   copy its entire value (one long string, e.g.
   "bcookie=...; li_at=...; JSESSIONID=...; lidc=...; ...").

2. TWO-COOKIE (fallback, less reliable): LI_AT_COOKIE + JSESSIONID
   only, from DevTools → Application/Storage → Cookies. Kept for
   simplicity, but more prone to the session-invalidation issue above.

3. PASSWORD-BASED (last resort): LINKEDIN_EMAIL + LINKEDIN_PASSWORD.
   This script POSTs credentials to LinkedIn's login endpoint itself.
   LinkedIn's anti-automation system flags scripted logins far more
   readily than it flags reusing an already-trusted session — expect
   CheckpointError here often. Prefer option 1.
"""

import requests

LOGIN_URL = "https://www.linkedin.com/checkpoint/lg/login-submit"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class AuthError(Exception):
    """Login itself failed (bad credentials, bad cookies, or unexpected response)."""
    pass


class CheckpointError(AuthError):
    """LinkedIn presented a 2FA/CAPTCHA/'verify it's you' challenge."""
    pass


class RateLimitedError(Exception):
    """LinkedIn returned 999/429 — account is being throttled or flagged."""
    pass


class ProfileNotFoundError(Exception):
    """The requested profile doesn't exist or isn't visible to this account."""
    pass


class LinkedInSession:
    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        li_at_cookie: str | None = None,
        jsessionid: str | None = None,
        cookie_string: str | None = None,
        user_agent: str | None = None,
    ):
        self.email = email
        self.password = password
        self.li_at_cookie = li_at_cookie
        self.jsessionid = jsessionid
        self.cookie_string = cookie_string
        self.session = requests.Session()
        # CRITICAL: this must match the real browser the cookies came from.
        # LinkedIn appears to bind a session to the device/browser it was
        # issued to; sending requests with a generic/mismatched User-Agent
        # looks like the session being used from a different device, and
        # LinkedIn can defensively invalidate the session in response —
        # which also explains the original browser tab getting logged out.
        # This mirrors PhantomBuster's own documented requirement to supply
        # your real user agent alongside the session cookie.
        self.session.headers.update({
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "X-Li-Lang": "en_US",
            "X-Restli-Protocol-Version": "2.0.0",
        })
        self._authenticated = False

    def _current_csrf_token(self) -> str:
        """
        Always derive the CSRF token from whatever JSESSIONID cookie the
        session currently holds, rather than a value captured once at
        login. LinkedIn can rotate JSESSIONID via Set-Cookie on responses
        (requests' cookie jar updates automatically) — using a stale
        captured token causes a CSRF mismatch on later requests, which
        LinkedIn answers with a redirect that looks like "session expired"
        even though the underlying li_at session is still fine.
        """
        current = self.session.cookies.get("JSESSIONID")
        if not current:
            raise AuthError("No JSESSIONID cookie present — not logged in.")
        return current.strip('"')

    def login(self):
        if self.cookie_string:
            self._login_with_cookie_string()
        elif self.li_at_cookie and self.jsessionid:
            self._login_with_cookies()
        elif self.email and self.password:
            self._login_with_password()
        else:
            raise AuthError(
                "No credentials provided. Set LINKEDIN_COOKIE_STRING (preferred), "
                "or LI_AT_COOKIE + JSESSIONID, or LINKEDIN_EMAIL + LINKEDIN_PASSWORD."
            )

    def _load_cookies_and_verify(self):
        """Shared by both cookie-based login paths: confirm the loaded
        cookies actually authenticate, by making one cheap real call."""
        headers = {"csrf-token": self._current_csrf_token(), "Referer": "https://www.linkedin.com/"}
        # allow_redirects=False on purpose (see get()/get_html() for the
        # same reasoning): invalid/incomplete cookies get redirected
        # toward a login page, which can loop until requests gives up
        # with an unhelpful "Exceeded 30 redirects" crash instead of a
        # clear error — confirmed happening on a real deployment.
        r = self.session.get(
            "https://www.linkedin.com/voyager/api/me", headers=headers, timeout=15, allow_redirects=False
        )
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            raise AuthError(
                "LinkedIn redirected instead of authenticating — the cookies "
                "are likely invalid, incomplete, or expired (or, if this is "
                "the very first request from a new deployment/IP, LinkedIn "
                "may be treating the session as untrusted from that origin). "
                "Verify LINKEDIN_COOKIE_STRING is the complete, unmodified "
                "value copied from DevTools, or re-copy fresh cookies."
            )
        if r.status_code != 200:
            raise AuthError(
                f"Provided cookies did not authenticate (status {r.status_code}). "
                "They may be expired, or copied incorrectly — log into LinkedIn "
                "again in your browser and re-copy fresh cookies."
            )
        self._authenticated = True

    def _login_with_cookie_string(self):
        # Parse a raw "Cookie:" header value, e.g.
        # "bcookie=v=2&abc; li_at=XYZ; JSESSIONID=\"ajax:123\"; lidc=..."
        for part in self.cookie_string.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            self.session.cookies.set(name.strip(), value.strip(), domain=".linkedin.com")

        if not self.session.cookies.get("li_at") or not self.session.cookies.get("JSESSIONID"):
            raise AuthError(
                "LINKEDIN_COOKIE_STRING didn't contain li_at and/or JSESSIONID — "
                "make sure you copied the full Cookie header value, not a partial one."
            )
        self._load_cookies_and_verify()

    def _login_with_cookies(self):
        # Normalize: JSESSIONID from devtools includes surrounding quotes,
        # e.g. "ajax:1234567890" — the cookie jar wants it exactly as
        # LinkedIn set it, but the csrf-token header wants it unquoted.
        jsessionid_raw = self.jsessionid if self.jsessionid.startswith('"') else f'"{self.jsessionid}"'

        self.session.cookies.set("li_at", self.li_at_cookie, domain=".linkedin.com")
        self.session.cookies.set("JSESSIONID", jsessionid_raw, domain=".linkedin.com")
        self._load_cookies_and_verify()

    def _login_with_password(self):
        r = self.session.get("https://www.linkedin.com/login", timeout=15)
        r.raise_for_status()

        jsessionid = self.session.cookies.get("JSESSIONID")
        if not jsessionid:
            raise AuthError("Did not receive an initial JSESSIONID cookie — LinkedIn's login page structure may have changed.")

        csrf = jsessionid.strip('"')
        payload = {
            "session_key": self.email,
            "session_password": self.password,
            "JSESSIONID": jsessionid,
        }
        headers = {"csrf-token": csrf}

        r = self.session.post(LOGIN_URL, data=payload, headers=headers, timeout=15)

        if "checkpoint" in r.url or "challenge" in r.url:
            raise CheckpointError(
                "LinkedIn presented a security checkpoint (2FA / CAPTCHA / "
                "'verify it's you'). This can't be solved headlessly. Prefer "
                "the cookie-based login instead (see module docstring) — copy "
                "li_at and JSESSIONID from a normal browser session rather "
                "than scripting the login form."
            )

        if r.status_code != 200 or "li_at" not in self.session.cookies:
            raise AuthError(
                f"Login failed (status {r.status_code}). Check credentials, "
                "or LinkedIn's login endpoint/field names may have changed — "
                "compare against a real login in browser devtools."
            )

        self._authenticated = True

    def get(self, url: str, params: dict | None = None) -> dict:
        if not self._authenticated:
            raise AuthError("Not logged in — call login() first.")
        headers = {"csrf-token": self._current_csrf_token(), "Referer": "https://www.linkedin.com/"}

        # allow_redirects=False on purpose: an expired/invalid session gets
        # redirected toward a login page, which can loop indefinitely
        # against certain URLs. Catching the first redirect here turns that
        # into an immediate, clear error instead of a 30-redirect timeout.
        r = self.session.get(url, params=params, headers=headers, timeout=15, allow_redirects=False)

        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            self._authenticated = False
            raise AuthError(
                "Session appears to have expired mid-request (LinkedIn redirected "
                "instead of returning data). Log into LinkedIn again in your "
                "browser, copy fresh li_at/JSESSIONID cookies, update .env, and "
                "restart the server."
            )
        if r.status_code in (999, 429):
            raise RateLimitedError(
                f"LinkedIn throttled/blocked this account (status {r.status_code}). "
                "Back off and reduce request frequency."
            )
        if r.status_code == 404:
            raise ProfileNotFoundError("Profile not found, or not visible to this account.")
        r.raise_for_status()
        return r.json()

    def get_html(self, url: str) -> str:
        """
        Same as get(), but for a plain profile page (not a JSON API
        endpoint). LinkedIn's newer profile UI server-renders the topcard
        (name/headline/school/location) directly into the page HTML, and
        as of this project's development, dedicated /details/<section>/
        pages also carry a componentkey embedding the profile's internal
        ID (see app/voyager_client.py's extract_profile_id_from_html) —
        used to call the pagination action endpoint for sections like
        education that don't render their data inline. See
        app/voyager_client.py for what this is used for.
        """
        if not self._authenticated:
            raise AuthError("Not logged in — call login() first.")
        headers = {"Referer": "https://www.linkedin.com/"}
        r = self.session.get(url, headers=headers, timeout=15, allow_redirects=False)

        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            self._authenticated = False
            raise AuthError(
                "Session appears to have expired mid-request (LinkedIn redirected "
                "instead of returning the page). Log into LinkedIn again in your "
                "browser, copy fresh li_at/JSESSIONID cookies, update .env, and "
                "restart the server."
            )
        if r.status_code in (999, 429):
            raise RateLimitedError(
                f"LinkedIn throttled/blocked this account (status {r.status_code}). "
                "Back off and reduce request frequency."
            )
        if r.status_code == 404:
            raise ProfileNotFoundError("Profile not found, or not visible to this account.")
        r.raise_for_status()
        # Force UTF-8 explicitly: LinkedIn doesn't always declare a charset
        # in Content-Type, and requests' auto-detected fallback (Latin-1
        # per HTTP spec when no charset is given) mangles special
        # characters like em-dashes and middots into mojibake — confirmed
        # live (garbled "—" and "·" in experience text before this fix).
        r.encoding = "utf-8"
        return r.text

    def post_json(self, url: str, json_body: dict, referer: str, extra_headers: dict | None = None) -> str:
        """
        POST with a JSON body, for the SDUI "pagination" action endpoint
        (flagship-web/rsc-action/actions/pagination). Unlike get()/get_html(),
        this also needs x-li-rsc-stream: true to get the RSC-stream format
        back, and a couple of x-li-* tracking headers LinkedIn's own client
        sends — reverse-engineered from a real captured request (see README
        "Approach" for how this endpoint was found). Returns raw response
        text (the RSC-stream format, parsed by app/voyager_client.py).
        """
        if not self._authenticated:
            raise AuthError("Not logged in — call login() first.")
        headers = {
            "Content-Type": "application/json",
            "csrf-token": self._current_csrf_token(),
            "Origin": "https://www.linkedin.com",
            "Referer": referer,
            "x-li-rsc-stream": "true",
        }
        if extra_headers:
            headers.update(extra_headers)

        r = self.session.post(url, json=json_body, headers=headers, timeout=15, allow_redirects=False)

        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            self._authenticated = False
            raise AuthError(
                "Session appears to have expired mid-request (LinkedIn redirected "
                "instead of returning data). Log in again and refresh cookies."
            )
        if r.status_code in (999, 429):
            raise RateLimitedError(
                f"LinkedIn throttled/blocked this account (status {r.status_code}). "
                "Back off and reduce request frequency."
            )
        r.raise_for_status()
        r.encoding = "utf-8"  # see note in get_html() above
        return r.text