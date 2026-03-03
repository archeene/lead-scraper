"""
Spark Membership booking (IMA Westborough).

Auth: ASP.NET WebForms login (reuses pattern from scrapers/spark.py).
Booking: Calendar.ashx saveAppointment endpoint.
"""

import logging
import re

import httpx

from app.config import settings
from app.bookers.models import BookingRequest, BookingResponse

log = logging.getLogger(__name__)

BASE_URL = "https://app.sparkmembership.com"
LOGIN_URL = f"{BASE_URL}/login.aspx"
CALENDAR_API = f"{BASE_URL}/Calendar.ashx"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


async def _get_session() -> httpx.AsyncClient:
    """Log in to Spark and return an authenticated client."""
    client = httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": UA},
        timeout=30,
    )

    resp = await client.get(LOGIN_URL)
    html = resp.text

    viewstate: dict[str, str] = {}
    for name in (
        "__VIEWSTATE",
        "__VIEWSTATEGENERATOR",
        "__EVENTVALIDATION",
        "__EVENTTARGET",
        "__EVENTARGUMENT",
    ):
        match = re.search(rf'id="{name}"\s+value="([^"]*)"', html)
        if match:
            viewstate[name] = match.group(1)

    hlogin_match = re.search(r'id="hLogin"[^>]*value="([^"]*)"', html)

    if not viewstate.get("__VIEWSTATE"):
        await client.aclose()
        raise RuntimeError("Spark: could not extract __VIEWSTATE from login page")

    form_data = {
        **viewstate,
        "hLogin": hlogin_match.group(1) if hlogin_match else "",
        "txtEmail": settings.spark_email,
        "txtPass": settings.spark_password,
        "btnLogin": "Login",
    }

    resp = await client.post(LOGIN_URL, data=form_data)

    if "login.aspx" in str(resp.url).lower() and "btnLogin" in resp.text:
        await client.aclose()
        raise RuntimeError("Spark: login failed, still on login page")

    log.info("Spark: logged in, cookies: %s", list(client.cookies.keys()))
    return client


async def create_booking(req: BookingRequest, loc_config: dict) -> BookingResponse:
    """
    Create an appointment in Spark via Calendar.ashx saveAppointment.

    The Calendar.ashx endpoint accepts various actions:
      - getEvents: list appointments
      - saveAppointment: create/update appointment
      - deleteAppointment: cancel
    """
    client = None
    try:
        client = await _get_session()

        # Build the appointment payload for Calendar.ashx
        # Spark uses a specific format for the saveAppointment action
        appointment_data = {
            "action": "saveAppointment",
            "appointmentDate": req.requested_date,
            "appointmentTime": req.requested_time,
            "clientName": req.customer_name,
            "clientEmail": req.customer_email or "",
            "clientPhone": req.customer_phone or "",
            "notes": req.notes or f"Booked via {req.source}",
        }

        if req.instructor:
            appointment_data["trainer"] = req.instructor
        if req.class_name:
            appointment_data["serviceName"] = req.class_name

        resp = await client.post(
            CALENDAR_API,
            data=appointment_data,
            timeout=30,
        )

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:500]}

            # Check for success indicators in response
            success = False
            booking_id = None

            if isinstance(data, dict):
                # Common success patterns
                if data.get("success") or data.get("Success"):
                    success = True
                    booking_id = str(
                        data.get("appointmentId", data.get("id", data.get("eventId", "")))
                    )
                elif data.get("error") or data.get("Error"):
                    return BookingResponse(
                        success=False,
                        message=f"Spark error: {data.get('error', data.get('Error', 'Unknown'))}",
                        errors=[str(data)],
                    )
                else:
                    # No explicit success/error field: treat 200 as success
                    success = True
                    booking_id = str(data.get("appointmentId", data.get("id", "")))

            if success:
                log.info("Spark: appointment created, id=%s", booking_id)
                return BookingResponse(
                    success=True,
                    booking_id=booking_id or None,
                    message="Booking confirmed in Spark",
                )

        error_text = resp.text[:500]
        log.error("Spark: saveAppointment failed: %d %s", resp.status_code, error_text)
        return BookingResponse(
            success=False,
            message=f"Spark booking failed ({resp.status_code})",
            errors=[error_text],
        )

    except Exception as e:
        log.error("Spark booking error: %s", e)
        return BookingResponse(
            success=False,
            message=f"Spark booking error: {e}",
            errors=[str(e)],
        )
    finally:
        if client:
            await client.aclose()
