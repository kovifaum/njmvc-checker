#!/usr/bin/env python3
"""
NJ MVC REAL ID Appointment Checker & Auto-Booker
Monitors Toms River (locationId=134) and Howell (locationId=135) for March 2026 openings.
Sends Gmail alerts and optionally auto-books via 2captcha.

Usage:
    python3 njmvc_checker.py

Config file (njmvc_config.json) must be in the same directory.
"""

import json
import os
import sys
import time
import smtplib
import requests
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
BASE_URL        = "https://telegov.njportal.com"
APPOINTMENT_ID  = 12          # Real ID service type
RECAPTCHA_KEY   = "6LfpgswZAAAAAFDVLD6UwyKXqw7WpyK3vsTXGgR6"

LOCATIONS = {
    134: {
        "name":    "Toms River",
        "book_url": f"{BASE_URL}/njmvc/AppointmentWizard/12/134",
    },
    135: {
        "name":    "Howell",
        "book_url": f"{BASE_URL}/njmvc/AppointmentWizard/12/135",
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

# ─────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────

def load_config() -> dict:
    # ── Local mode: read from njmvc_config.json ──
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "njmvc_config.json")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)

    # ── Cloud mode: read from environment variables (GitHub Actions / Render / etc.) ──
    applicants_raw = os.environ.get("APPLICANTS", "")
    if not applicants_raw:
        print("[ERROR] No njmvc_config.json found and no APPLICANTS env var set.")
        sys.exit(1)
    return {
        "gmail_address":      os.environ["GMAIL_ADDRESS"],
        "gmail_app_password": os.environ["GMAIL_APP_PASSWORD"],
        "notification_email": os.environ["NOTIFICATION_EMAIL"],
        "sms_address":        os.environ.get("SMS_ADDRESS", ""),
        "auto_book":          os.environ.get("AUTO_BOOK", "true").lower() == "true",
        "twocaptcha_api_key": os.environ.get("TWOCAPTCHA_API_KEY", ""),
        "applicants":         json.loads(applicants_raw),
    }

# ─────────────────────────────────────────────
# Slot discovery
# ─────────────────────────────────────────────

def get_available_dates(location_id: int, month_iso: str) -> list[str]:
    """Returns list of ISO timestamp strings available for the given month."""
    url = f"{BASE_URL}/njmvc/CustomerCreateAppointments/GetAvailableDatesForMonth"
    params = {
        "duration":      20,
        "locationId":    location_id,
        "appointmentId": APPOINTMENT_ID,
        "date":          month_iso,
    }
    hdrs = {
        **HEADERS,
        "Referer": f"{BASE_URL}/njmvc/AppointmentWizard/{APPOINTMENT_ID}/{location_id}",
    }
    try:
        r = requests.get(url, params=params, headers=hdrs, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] get_available_dates({location_id}): {e}")
        return []


def filter_march_no_saturday(dates: list[str]) -> list[date]:
    """Keep only dates in March 2026 that are NOT Saturdays (weekday==5)."""
    result = []
    for d in dates:
        try:
            dt = datetime.fromisoformat(d)
            if dt.year == 2026 and dt.month == 3 and dt.weekday() != 5:
                result.append(dt.date())
        except ValueError:
            pass
    return result


def token_to_minutes(token: str) -> int:
    """Convert a time token like '815' or '1415' to minutes since midnight."""
    token = token.strip()
    if len(token) <= 3:
        hour, minute = int(token[0]),   int(token[1:])
    else:
        hour, minute = int(token[:2]),  int(token[2:])
    return hour * 60 + minute

MIN_SLOT_MINUTES = 9 * 60 + 45  # 9:45 AM


def get_time_slots(session: requests.Session, location_id: int, date_str: str) -> list[str]:
    """
    Returns time-token strings (e.g. '1415' for 2:15 PM) from the date-selection page,
    filtered to only include slots at or after 9:45 AM.
    """
    url = f"{BASE_URL}/njmvc/AppointmentWizard/{APPOINTMENT_ID}/{location_id}?date={date_str}"
    hdrs = {
        **HEADERS,
        "Referer": f"{BASE_URL}/njmvc/AppointmentWizard/{APPOINTMENT_ID}/{location_id}",
    }
    try:
        r = session.get(url, headers=hdrs, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[WARN] get_time_slots({location_id}, {date_str}): {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    slots = []
    for a in soup.select("a.text-primary"):
        href = a.get("href", "")
        prefix = f"/njmvc/AppointmentWizard/{APPOINTMENT_ID}/{location_id}/{date_str}/"
        if href.startswith(prefix):
            token = href[len(prefix):]
            if token_to_minutes(token) >= MIN_SLOT_MINUTES:
                slots.append(token)
            else:
                print(f"  [SKIP] Slot {token} is before 9:45 AM")
    return slots

# ─────────────────────────────────────────────
# reCAPTCHA solver (2captcha)
# ─────────────────────────────────────────────

def solve_recaptcha(api_key: str, page_url: str, max_wait: int = 180) -> str:
    print("[INFO] Submitting reCAPTCHA to 2captcha...")
    try:
        resp = requests.post(
            "https://2captcha.com/in.php",
            data={"key": api_key, "method": "userrecaptcha", "googlekey": RECAPTCHA_KEY,
                  "pageurl": page_url, "json": 1},
            timeout=15,
        )
        data = resp.json()
        if data.get("status") != 1:
            print(f"[WARN] 2captcha submission failed: {data}")
            return ""
        captcha_id = data["request"]
    except Exception as e:
        print(f"[WARN] 2captcha submit error: {e}")
        return ""
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(5)
        elapsed += 5
        try:
            res = requests.get("https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": captcha_id, "json": 1}, timeout=10)
            r = res.json()
            if r.get("status") == 1:
                print("[INFO] reCAPTCHA solved.")
                return r["request"]
            if r.get("request") != "CAPCHA_NOT_READY":
                print(f"[WARN] 2captcha error: {r}")
                return ""
        except Exception as e:
            print(f"[WARN] 2captcha poll error: {e}")
    print("[WARN] 2captcha timed out.")
    return ""

# ─────────────────────────────────────────────
# Booking
# ─────────────────────────────────────────────

def _hidden(soup, name):
    el = soup.find("input", {"name": name})
    return el["value"] if el else ""

def attempt_booking(session, location_id, date_str, time_token, applicant, twocaptcha_key):
    slot_url = f"{BASE_URL}/njmvc/AppointmentWizard/{APPOINTMENT_ID}/{location_id}/{date_str}/{time_token}"
    hdrs = {**HEADERS, "Referer": f"{BASE_URL}/njmvc/AppointmentWizard/{APPOINTMENT_ID}/{location_id}?date={date_str}",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    hdrs.pop("X-Requested-With", None)
    try:
        r = session.get(slot_url, headers=hdrs, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return False, f"GET failed: {e}"
    soup = BeautifulSoup(r.text, "html.parser")
    slot_lock_id = _hidden(soup, "AppointmentSlotLockId")
    csrf_token = _hidden(soup, "__RequestVerificationToken")
    if not slot_lock_id:
        return False, "No slot lock ID - slot may be taken."
    if not twocaptcha_key:
        return False, "No 2captcha key."
    captcha_token = solve_recaptcha(twocaptcha_key, slot_url)
    if not captcha_token:
        return False, "reCAPTCHA failed."
    form_data = {
        "Id": _hidden(soup, "Id") or "0", "AppointmentTime": _hidden(soup, "AppointmentTime"),
        "AppointmentDate": _hidden(soup, "AppointmentDate"), "AppointmentTypeId": _hidden(soup, "AppointmentTypeId"),
        "LocationId": _hidden(soup, "LocationId"), "CustomerId": _hidden(soup, "CustomerId") or "0",
        "Customer.Id": _hidden(soup, "Customer.Id") or "0", "ConfirmationNumber": "",
        "AppointmentSlotLockId": slot_lock_id, "Customer.FirstName": applicant["first_name"],
        "Customer.LastName": applicant["last_name"], "Customer.Email": applicant["email"],
        "Customer.PhoneNumber": applicant["phone"], "driverLicense": applicant["license_number"],
        "Customer.ReceiveTexts": "false", "Attest": "true", "PtaAttest": "true",
        "__RequestVerificationToken": csrf_token, "g-recaptcha-response": captcha_token,
    }
    try:
        resp = session.post(slot_url, data=form_data,
            headers={**hdrs, "Content-Type": "application/x-www-form-urlencoded", "Referer": slot_url},
            timeout=30, allow_redirects=True)
    except Exception as e:
        return False, f"POST failed: {e}"
    if "confirmation" in resp.url.lower() or "Confirmation" in resp.text or "successfully" in resp.text.lower():
        return True, resp.url
    err_soup = BeautifulSoup(resp.text, "html.parser")
    errors = [e.get_text(strip=True) for e in err_soup.select(".text-danger, .alert-danger")]
    return False, " | ".join(errors) if errors else f"Unknown (status {resp.status_code})"

# ─────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────

def send_email(config, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["gmail_address"]
    msg["To"] = config["notification_email"]
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(config["gmail_address"], config["gmail_app_password"])
            srv.send_message(msg)
        print(f"[INFO] Email sent: {subject}")
    except Exception as e:
        print(f"[ERROR] Email failed: {e}")

def send_sms(config, text):
    sms_address = config.get("sms_address", "").strip()
    if not sms_address:
        return
    msg = MIMEText(text[:160])
    msg["Subject"] = ""
    msg["From"] = config["gmail_address"]
    msg["To"] = sms_address
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(config["gmail_address"], config["gmail_app_password"])
            srv.send_message(msg)
        print(f"[INFO] SMS sent to {sms_address}")
    except Exception as e:
        print(f"[ERROR] SMS failed: {e}")

def notify_found(config, findings):
    email_found(config, findings)
    parts = [f"{f['name']}: {', '.join(str(d) for d in f['dates'])}" for f in findings]
    send_sms(config, "NJ MVC MARCH SLOT OPEN! " + " | ".join(parts) + " - check email to book.")

def email_found(config, findings):
    rows = []
    for f in findings:
        dates_str = ", ".join(str(d) for d in f["dates"])
        rows.append(f"<tr><td style='padding:8px;border:1px solid #ddd'><b>{f['name']}</b></td>"
                    f"<td style='padding:8px;border:1px solid #ddd'>{dates_str}</td>"
                    f"<td style='padding:8px;border:1px solid #ddd'><a href='{f['book_url']}'>Book Now</a></td></tr>")
    body = (f"<div style='font-family:Arial,sans-serif;max-width:600px;margin:auto'>"
            f"<div style='background:#1F4A8F;padding:20px;border-radius:8px 8px 0 0'>"
            f"<h1 style='color:white;margin:0'>NJ MVC March Opening Found!</h1></div>"
            f"<div style='background:#f9f9f9;padding:20px;border:1px solid #ddd;border-top:none'>"
            f"<p>A March 2026 REAL ID slot opened. Act fast!</p>"
            f"<table style='width:100%;border-collapse:collapse'>{''.join(rows)}</table>"
            f"<p style='color:#666;font-size:12px'>Detected at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
            f"</div></div>")
    send_email(config, "NJ MVC REAL ID March Slot Found - Book NOW!", body)

def notify_booked(config, applicant, location_name, date_str, confirm_url):
    body = (f"<div style='font-family:Arial,sans-serif;max-width:600px;margin:auto'>"
            f"<div style='background:#2e7d32;padding:20px;border-radius:8px 8px 0 0'>"
            f"<h1 style='color:white;margin:0'>Appointment Booked!</h1></div>"
            f"<div style='background:#f9f9f9;padding:20px;border:1px solid #ddd;border-top:none'>"
            f"<p><b>Name:</b> {applicant['first_name']} {applicant['last_name']}<br>"
            f"<b>Location:</b> {location_name}<br><b>Date:</b> {date_str}<br>"
            f"<b>Confirmation:</b> <a href='{confirm_url}'>{confirm_url}</a></p></div></div>")
    send_email(config, f"NJ MVC Booked for {applicant['first_name']} {applicant['last_name']}!", body)
    send_sms(config, f"BOOKED! {applicant['first_name']} {applicant['last_name']} @ {location_name} on {date_str}.")

def notify_booking_failed(config, applicant, reason, book_url):
    body = (f"<div style='font-family:Arial,sans-serif;max-width:600px;margin:auto'>"
            f"<div style='background:#c62828;padding:20px;border-radius:8px 8px 0 0'>"
            f"<h1 style='color:white;margin:0'>Auto-Book Failed - Slot Found!</h1></div>"
            f"<div style='background:#f9f9f9;padding:20px;border:1px solid #ddd;border-top:none'>"
            f"<p>Slot found but booking failed for {applicant['first_name']} {applicant['last_name']}.<br>"
            f"<b>Reason:</b> {reason}<br>"
            f"<a href='{book_url}' style='background:#1F4A8F;color:white;padding:10px 20px;"
            f"border-radius:4px;text-decoration:none'>Book Manually</a></p></div></div>")
    send_email(config, f"NJ MVC Slot Found - Manual Booking Needed ({applicant['first_name']})", body)
    send_sms(config, f"Slot found, auto-book FAILED for {applicant['first_name']}. Book manually NOW!")

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    config = load_config()
    twocaptcha_key = config.get("twocaptcha_api_key", "").strip()
    auto_book = config.get("auto_book", False) and bool(twocaptcha_key)
    applicants = config.get("applicants", [])
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking NJ MVC REAL ID availability...")
    findings = []
    for loc_id, loc_info in LOCATIONS.items():
        dates_raw = get_available_dates(loc_id, "2026-03-01T00:00:00")
        march_dates = filter_march_no_saturday(dates_raw)
        if march_dates:
            findings.append({"location_id": loc_id, "name": loc_info["name"],
                             "dates": march_dates, "book_url": loc_info["book_url"]})
            print(f"  + {loc_info['name']}: {[str(d) for d in march_dates]}")
        else:
            print(f"  - {loc_info['name']}: no March openings")
    if not findings:
        print("No March openings at either location. Done.")
        return
    notify_found(config, findings)
    if not auto_book:
        print("[INFO] Notification sent. Auto-book disabled or no 2captcha key.")
        return
    if not applicants:
        print("[WARN] No applicants in config.")
        return
    session = requests.Session()
    for applicant in applicants:
        name_label = f"{applicant['first_name']} {applicant['last_name']}"
        booked = False
        for finding in findings:
            if booked: break
            for avail_date in finding["dates"]:
                if booked: break
                date_str = str(avail_date)
                print(f"[INFO] Booking {name_label} @ {finding['name']} on {date_str}")
                slots = get_time_slots(session, finding["location_id"], date_str)
                if not slots:
                    print(f"  No slots for {date_str}.")
                    continue
                print(f"  {len(slots)} slot(s), using: {slots[0]}")
                success, result = attempt_booking(session, finding["location_id"], date_str, slots[0], applicant, twocaptcha_key)
                if success:
                    print(f"  Booked! {result}")
                    notify_booked(config, applicant, finding["name"], date_str, result)
                else:
                    print(f"  Failed: {result}")
                    notify_booking_failed(config, applicant, result, finding["book_url"])
                booked = True
        if not booked:
            print(f"[WARN] No bookable slot for {name_label}.")

if __name__ == "__main__":
    main()
