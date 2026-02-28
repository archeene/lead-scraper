"""
ClubReady scraper (SZ Westborough).

3-step cookie chain auth + A-Z brute-force QuickSearch.
Filters to leads/prospects during scraping (skips members).
Uses httpx with retry logic (QuickSearch returns JSON, no browser needed).
"""

import asyncio
import logging
import re
import string

import httpx

from app.config import settings
from app.schemas import Lead, ScrapeResponse
from app.utils.normalize import normalize_phone, normalize_name, days_since, is_lead_status

log = logging.getLogger(__name__)

LOGIN_URL = "https://login.clubready.com/Security/Login"
SELECTOR_URL = "https://www.clubready.com/login/loginselector"
SECURITY_URL = "https://www.clubready.com/Security/Login"
QUICKSEARCH_URL = "https://app.clubready.com/Users/Lookup/QuickSearch"

BATCH_SIZE = 25
MAX_RETRIES = 3
RETRY_DELAY = 1.0

LEAD_STATUSES = {"lead", "prospect", "inquiry", "trial", "intro", "guest"}


async def _auth(client: httpx.AsyncClient) -> None:
    """3-step ClubReady login to establish session cookies."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # Step 1: POST to login.clubready.com
    resp = await client.post(
        LOGIN_URL,
        data={"UserName": settings.cr_username, "Password": settings.cr_password},
        headers=headers,
        follow_redirects=True,
        timeout=30,
    )
    # Extract token from redirect URL or response
    token = None
    uid = None

    # Token might be in the redirect URL params
    url_str = str(resp.url)
    token_match = re.search(r'[?&]token=([^&]+)', url_str)
    if token_match:
        token = token_match.group(1)

    # Or in hidden form fields
    token_match = token_match or re.search(r'name="token"\s+value="([^"]+)"', resp.text)
    if not token and token_match:
        token = token_match.group(1)

    # Or in JSON response
    if not token:
        try:
            data = resp.json()
            token = data.get("token", data.get("Token"))
            uid = data.get("uid", data.get("UID", data.get("userId")))
        except Exception:
            pass

    # Try extracting from the response body
    if not token:
        token_match = re.search(r'"[Tt]oken"\s*:\s*"([^"]+)"', resp.text)
        if token_match:
            token = token_match.group(1)

    if not token:
        raise RuntimeError("ClubReady auth step 1 failed: no token in response")

    # Extract UID if not found yet
    if not uid:
        uid_match = re.search(r'"(?:uid|UID|userId|UserId)"\s*:\s*"?(\d+)"?', resp.text)
        if uid_match:
            uid = uid_match.group(1)

    log.info("ClubReady: step 1 complete, token=%s...", token[:20] if token else "None")

    # Step 2: POST to loginselector with token + StoreId
    resp = await client.post(
        SELECTOR_URL,
        data={"token": token, "StoreId": settings.cr_store_id},
        headers=headers,
        follow_redirects=True,
        timeout=30,
    )
    log.info("ClubReady: step 2 complete, status=%d", resp.status_code)

    # Step 3: POST to Security/Login with token + UID
    form_data = {"token": token}
    if uid:
        form_data["UID"] = uid
    form_data["StoreId"] = settings.cr_store_id

    resp = await client.post(
        SECURITY_URL,
        data=form_data,
        headers=headers,
        follow_redirects=True,
        timeout=30,
    )
    log.info("ClubReady: step 3 complete, cookies: %s", list(client.cookies.keys()))


async def _quicksearch(
    client: httpx.AsyncClient, prefix: str, retries: int = MAX_RETRIES
) -> list[dict]:
    """Run a single QuickSearch query with retry."""
    for attempt in range(retries):
        try:
            resp = await client.get(
                QUICKSEARCH_URL,
                params={"searchText": prefix, "searchType": "1"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, list) else []
            if resp.status_code == 429:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return []
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    return []


def _is_cr_lead(member: dict) -> bool:
    """Check if a ClubReady QuickSearch result is a lead/prospect."""
    status_text = str(member.get("customerStatusText", "")).lower().strip()
    if status_text in LEAD_STATUSES:
        return True

    # customerStatus == 3 is typically lead in ClubReady
    status_id = member.get("customerStatus")
    if status_id in (3, "3"):
        return True

    return False


def _extract_cr_contact_date(member: dict) -> str | None:
    """Extract last contact/activity date from ClubReady QuickSearch fields."""
    for field in (
        "lastContactDate", "lastActivityDate", "lastVisitDate",
        "lastModifiedDate", "createdDate", "addedDate",
    ):
        val = member.get(field)
        if val and str(val).strip() not in ("", "null", "None", "0"):
            return str(val).strip()
    return None


async def scrape_clubready() -> ScrapeResponse:
    errors: list[str] = []
    lead_map: dict[int, dict] = {}
    queries = 0
    letters = string.ascii_lowercase

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    ) as client:
        try:
            await _auth(client)
        except Exception as e:
            return ScrapeResponse(
                leadCount=0, leads=[], errors=[f"Auth failed: {e}"], metadata={}
            )

        # Pass 1: 3-letter prefixes (26 * 26 = 676 per first letter = 17,576 total)
        capped: list[str] = []

        for c1 in letters:
            prefixes = [c1 + c2 + c3 for c2 in letters for c3 in letters]

            for i in range(0, len(prefixes), BATCH_SIZE):
                batch = prefixes[i : i + BATCH_SIZE]
                results = await asyncio.gather(
                    *[_quicksearch(client, p) for p in batch]
                )

                for j, result_list in enumerate(results):
                    for member in result_list:
                        uid = member.get("userId")
                        if uid and uid not in lead_map and _is_cr_lead(member):
                            lead_map[uid] = member
                    if len(result_list) >= 50:
                        capped.append(batch[j])

                queries += len(batch)

            log.info(
                "ClubReady: letter '%s' done, leads so far: %d, queries: %d, capped: %d",
                c1, len(lead_map), queries, len(capped),
            )

        # Pass 2: drill capped prefixes to 4 letters
        if capped:
            log.info("ClubReady: drilling %d capped prefixes to 4 letters", len(capped))
            drill_prefixes = [base + c4 for base in capped for c4 in letters]

            for i in range(0, len(drill_prefixes), BATCH_SIZE):
                batch = drill_prefixes[i : i + BATCH_SIZE]
                results = await asyncio.gather(
                    *[_quicksearch(client, p) for p in batch]
                )
                for result_list in results:
                    for member in result_list:
                        uid = member.get("userId")
                        if uid and uid not in lead_map and _is_cr_lead(member):
                            lead_map[uid] = member
                queries += len(batch)

        log.info("ClubReady: scan complete. %d leads, %d queries", len(lead_map), queries)

        # Convert to Lead objects with staleness filter
        leads: list[Lead] = []
        for uid, member in lead_map.items():
            last_contact = _extract_cr_contact_date(member)
            days = days_since(last_contact)

            # Only include stale leads
            if days is not None and days < settings.stale_days:
                continue

            first = member.get("firstName", member.get("name", ""))
            last = member.get("lastName", "")

            # Handle combined name field
            if not last and " " in first:
                parts = first.split(None, 1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""

            leads.append(Lead(
                id=str(uid),
                firstName=normalize_name(first),
                lastName=normalize_name(last),
                email=member.get("email"),
                phone=normalize_phone(member.get("phone", member.get("cellPhone"))),
                status=str(member.get("customerStatusText", "lead")).lower(),
                lastContactDate=last_contact,
                daysSinceContact=days,
                source="clubready",
            ))

    return ScrapeResponse(
        leadCount=len(leads),
        leads=leads,
        errors=errors,
        metadata={
            "queriesRun": queries,
            "totalRaw": len(lead_map),
            "filteredTo": len(leads),
            "cappedPrefixes": len(capped),
        },
    )
