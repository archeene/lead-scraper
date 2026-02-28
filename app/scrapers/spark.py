"""
Spark Membership scraper (IMA Westborough).

ASP.NET WebForms: ViewState auth + Contacts.aspx HTML table parsing.
Uses Scrapling Fetcher for TLS fingerprint impersonation and session persistence.
"""

import logging
import re
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.schemas import Lead, ScrapeResponse
from app.utils.normalize import normalize_phone, normalize_name, days_since

log = logging.getLogger(__name__)

BASE_URL = "https://app.sparkmembership.com"
LOGIN_URL = f"{BASE_URL}/login.aspx"
DASHBOARD_URL = f"{BASE_URL}/Dashboard.aspx"
CONTACTS_URL = f"{BASE_URL}/Contacts.aspx"


def _extract_viewstate(html: str) -> dict[str, str]:
    """Extract ASP.NET hidden fields from HTML."""
    fields = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        match = re.search(
            rf'id="{name}"\s+value="([^"]*)"', html
        )
        if match:
            fields[name] = match.group(1)
    return fields


def _parse_contacts_table(html: str) -> list[dict]:
    """Parse contacts from Spark's HTML. Tries multiple strategies."""
    contacts: list[dict] = []

    # Strategy 1: Look for table rows with data attributes or grid rows
    # Spark uses ASP.NET GridView which renders as <table> with <tr> rows
    table_match = re.search(
        r'<table[^>]*id="[^"]*(?:grid|Grid|contacts|Contacts)[^"]*"[^>]*>(.*?)</table>',
        html, re.DOTALL | re.IGNORECASE
    )

    if table_match:
        table_html = table_match.group(1)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)

        # First row is usually headers
        headers: list[str] = []
        if rows:
            header_cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', rows[0], re.DOTALL | re.IGNORECASE)
            headers = [re.sub(r'<[^>]+>', '', c).strip().lower() for c in header_cells]
            rows = rows[1:]  # skip header row

        for row_html in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL | re.IGNORECASE)
            cell_texts = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

            if not cell_texts or len(cell_texts) < 3:
                continue

            contact: dict = {}
            for i, header in enumerate(headers):
                if i < len(cell_texts):
                    val = cell_texts[i]
                    if "first" in header or header == "name":
                        # If combined name, split it
                        if "last" not in header and " " in val:
                            parts = val.split(None, 1)
                            contact["firstName"] = parts[0]
                            contact["lastName"] = parts[1] if len(parts) > 1 else ""
                        else:
                            contact["firstName"] = val
                    elif "last" in header:
                        contact["lastName"] = val
                    elif "email" in header:
                        contact["email"] = val
                    elif "phone" in header or "mobile" in header:
                        contact["phone"] = val
                    elif "type" in header or "status" in header:
                        contact["status"] = val
                    elif "date" in header or "last" in header or "activity" in header:
                        contact["lastDate"] = val
                    elif "id" == header:
                        contact["id"] = val

            if contact.get("firstName") or contact.get("email"):
                contacts.append(contact)

    # Strategy 2: Look for JSON data in script tags (some ASP.NET grids serialize data)
    if not contacts:
        json_match = re.search(r'var\s+\w*[Cc]ontacts?\w*\s*=\s*(\[.*?\]);', html, re.DOTALL)
        if json_match:
            import json
            try:
                data = json.loads(json_match.group(1))
                for item in data:
                    if isinstance(item, dict):
                        contacts.append(item)
            except (json.JSONDecodeError, ValueError):
                pass

    # Strategy 3: Simple row-based fallback (look for any table with enough columns)
    if not contacts:
        all_tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL | re.IGNORECASE)
        for table_html in all_tables:
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
            if len(rows) < 2:
                continue
            for row_html in rows[1:]:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL | re.IGNORECASE)
                cell_texts = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                if len(cell_texts) >= 3:
                    contact = {
                        "firstName": cell_texts[0] if len(cell_texts) > 0 else "",
                        "lastName": cell_texts[1] if len(cell_texts) > 1 else "",
                        "email": "",
                        "phone": "",
                    }
                    # Try to find email-like and phone-like cells
                    for ct in cell_texts:
                        if "@" in ct and not contact.get("email"):
                            contact["email"] = ct
                        elif re.match(r'[\d\(\)\-\s\+]{7,}', ct) and not contact.get("phone"):
                            contact["phone"] = ct
                    if contact["firstName"]:
                        contacts.append(contact)

    return contacts


async def _login(client: httpx.AsyncClient) -> None:
    """Perform ASP.NET ViewState login flow."""
    # Step 1: GET the dashboard/login page to get ViewState
    resp = await client.get(DASHBOARD_URL, follow_redirects=True, timeout=30)
    html = resp.text
    viewstate = _extract_viewstate(html)

    if not viewstate.get("__VIEWSTATE"):
        # Try the login page directly
        resp = await client.get(LOGIN_URL, follow_redirects=True, timeout=30)
        html = resp.text
        viewstate = _extract_viewstate(html)

    if not viewstate.get("__VIEWSTATE"):
        raise RuntimeError("Could not extract __VIEWSTATE from Spark login page")

    # Step 2: POST login with credentials + ViewState
    form_data = {
        **viewstate,
        "ctl00$MainContent$LoginUser$UserName": settings.spark_email,
        "ctl00$MainContent$LoginUser$Password": settings.spark_password,
        "ctl00$MainContent$LoginUser$LoginButton": "Log In",
    }

    resp = await client.post(LOGIN_URL, data=form_data, follow_redirects=True, timeout=30)

    # Verify login succeeded (should redirect to dashboard, not back to login)
    if "login.aspx" in str(resp.url).lower() and "LoginUser" in resp.text:
        raise RuntimeError("Spark login failed: still on login page after POST")

    log.info("Spark: logged in successfully, cookies: %s", list(client.cookies.keys()))


async def _scrape_contacts(client: httpx.AsyncClient, contact_type: str) -> list[dict]:
    """Fetch and parse contacts of a given type (L=Leads, P=Prospects)."""
    url = f"{CONTACTS_URL}?contactType={contact_type}"
    resp = await client.get(url, follow_redirects=True, timeout=30)

    if resp.status_code != 200:
        log.warning("Spark: %s returned status %d", url, resp.status_code)
        return []

    contacts = _parse_contacts_table(resp.text)
    log.info("Spark: parsed %d contacts from contactType=%s", len(contacts), contact_type)
    return contacts


async def scrape_spark() -> ScrapeResponse:
    errors: list[str] = []
    leads: list[Lead] = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    ) as client:
        try:
            await _login(client)
        except Exception as e:
            return ScrapeResponse(
                leadCount=0, leads=[], errors=[f"Login failed: {e}"], metadata={}
            )

        all_contacts: list[dict] = []

        # Scrape both leads and prospects
        for ctype in ("L", "P"):
            try:
                contacts = await _scrape_contacts(client, ctype)
                all_contacts.extend(contacts)
            except Exception as e:
                errors.append(f"contactType={ctype}: {e}")

        # Deduplicate by email or name combo
        seen: set[str] = set()
        total_raw = len(all_contacts)

        for i, c in enumerate(all_contacts):
            dedup_key = c.get("email", "") or f"{c.get('firstName', '')}_{c.get('lastName', '')}".lower()
            if dedup_key in seen or not dedup_key:
                continue
            seen.add(dedup_key)

            last_date = c.get("lastDate", c.get("last_activity", c.get("date")))
            days = days_since(last_date)

            # Only include stale leads (last contact > threshold, or no date)
            if days is not None and days < settings.stale_days:
                continue

            leads.append(Lead(
                id=c.get("id", str(i)),
                firstName=normalize_name(c.get("firstName", "")),
                lastName=normalize_name(c.get("lastName", "")),
                email=c.get("email"),
                phone=normalize_phone(c.get("phone")),
                status=c.get("status", "lead").lower() if c.get("status") else "lead",
                lastContactDate=last_date,
                daysSinceContact=days,
                source="spark",
            ))

    return ScrapeResponse(
        leadCount=len(leads),
        leads=leads,
        errors=errors,
        metadata={"totalRaw": total_raw, "filteredTo": len(leads)},
    )
