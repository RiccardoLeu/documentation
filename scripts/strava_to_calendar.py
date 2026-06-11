#!/usr/bin/env python3
"""
Strava → Google Calendar sync script.

Fetches recent Strava activities and creates corresponding events
on Google Calendar. Supports incremental sync via a local state file.

Setup:
  1. Create a Strava API application at https://www.strava.com/settings/api
     and note the Client ID and Client Secret.
  2. Run once with --authorize to complete the OAuth2 flow and save tokens.
  3. Create a Google Cloud project, enable the Calendar API, and download
     credentials.json (OAuth 2.0 Desktop client) to this directory.
  4. Set the STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET environment variables
     (or edit the CONFIG section below).
  5. Run: python strava_to_calendar.py

Dependencies:
  pip install requests google-auth google-auth-oauthlib google-api-python-client
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# CONFIG — override via environment variables or edit directly
# ---------------------------------------------------------------------------
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
STRAVA_TOKEN_FILE = Path(__file__).parent / ".strava_tokens.json"
GOOGLE_TOKEN_FILE = Path(__file__).parent / ".google_tokens.json"
GOOGLE_CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
SYNC_STATE_FILE = Path(__file__).parent / ".strava_sync_state.json"
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
# How many past activities to fetch on first run (max 200)
INITIAL_ACTIVITY_COUNT = 30
# Google Calendar ID — "primary" uses the default calendar
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# In CI, credentials are passed as JSON strings via env vars instead of files
STRAVA_TOKENS_JSON = os.environ.get("STRAVA_TOKENS_JSON", "")
GOOGLE_TOKENS_JSON = os.environ.get("GOOGLE_TOKENS_JSON", "")

# Activity type → emoji prefix for event title
ACTIVITY_EMOJI = {
    "Run": "🏃",
    "Ride": "🚴",
    "Swim": "🏊",
    "Walk": "🚶",
    "Hike": "🥾",
    "WeightTraining": "🏋️",
    "Yoga": "🧘",
    "Workout": "💪",
}

# ---------------------------------------------------------------------------
# Strava OAuth helpers
# ---------------------------------------------------------------------------

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"


def strava_authorize():
    """Print the authorization URL and exchange the code for tokens."""
    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
        print("ERROR: Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET env vars first.")
        sys.exit(1)

    auth_url = (
        f"{STRAVA_AUTH_URL}?client_id={STRAVA_CLIENT_ID}"
        "&response_type=code"
        "&redirect_uri=http://localhost"
        "&approval_prompt=force"
        "&scope=activity:read_all"
    )
    print("Open this URL in your browser and authorize the app:\n")
    print(auth_url)
    print()
    code = input("Paste the 'code' parameter from the redirect URL: ").strip()

    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    tokens = resp.json()
    STRAVA_TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    print(f"Tokens saved to {STRAVA_TOKEN_FILE}")


def strava_refresh_tokens(tokens: dict) -> dict:
    """Refresh the Strava access token if expired."""
    if tokens.get("expires_at", 0) > datetime.now(timezone.utc).timestamp() + 60:
        return tokens

    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    resp.raise_for_status()
    new_tokens = resp.json()
    STRAVA_TOKEN_FILE.write_text(json.dumps(new_tokens, indent=2))
    return new_tokens


def get_strava_headers() -> dict:
    """Return Authorization headers with a valid access token."""
    if STRAVA_TOKENS_JSON:
        tokens = json.loads(STRAVA_TOKENS_JSON)
    elif STRAVA_TOKEN_FILE.exists():
        tokens = json.loads(STRAVA_TOKEN_FILE.read_text())
    else:
        print("No Strava tokens found. Run with --authorize first.")
        sys.exit(1)
    tokens = strava_refresh_tokens(tokens)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def fetch_strava_activities(after_timestamp: int = 0) -> list[dict]:
    """Fetch activities newer than after_timestamp (Unix time)."""
    headers = get_strava_headers()
    activities = []
    page = 1
    per_page = 50

    while True:
        params = {"per_page": per_page, "page": page}
        if after_timestamp:
            params["after"] = after_timestamp
        else:
            params["per_page"] = INITIAL_ACTIVITY_COUNT

        resp = requests.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < per_page or not after_timestamp:
            break
        page += 1

    return activities


# ---------------------------------------------------------------------------
# Google Calendar helpers
# ---------------------------------------------------------------------------


def get_google_calendar_service():
    """Authenticate and return a Google Calendar API service client."""
    creds = None

    if GOOGLE_TOKENS_JSON:
        creds = Credentials.from_authorized_user_info(
            json.loads(GOOGLE_TOKENS_JSON), GOOGLE_CALENDAR_SCOPES
        )
    elif GOOGLE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(GOOGLE_TOKEN_FILE), GOOGLE_CALENDAR_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GOOGLE_CREDENTIALS_FILE.exists():
                print(
                    f"ERROR: {GOOGLE_CREDENTIALS_FILE} not found.\n"
                    "Download OAuth2 Desktop credentials from Google Cloud Console."
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GOOGLE_CREDENTIALS_FILE), GOOGLE_CALENDAR_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GOOGLE_TOKEN_FILE.write_text(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def activity_to_calendar_event(activity: dict) -> dict:
    """Convert a Strava activity dict into a Google Calendar event body."""
    activity_type = activity.get("type", "Workout")
    emoji = ACTIVITY_EMOJI.get(activity_type, "🏅")

    name = activity.get("name", activity_type)
    summary = f"{emoji} {name}"

    start_dt = activity["start_date_local"]  # ISO 8601, local time
    elapsed = activity.get("elapsed_time", 0)  # seconds
    start = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
    from datetime import timedelta
    end = start + timedelta(seconds=elapsed)

    distance_m = activity.get("distance", 0)
    distance_km = distance_m / 1000
    avg_speed = activity.get("average_speed", 0)  # m/s
    avg_hr = activity.get("average_heartrate")
    elevation = activity.get("total_elevation_gain", 0)

    lines = [f"Type: {activity_type}"]
    if distance_km > 0:
        lines.append(f"Distance: {distance_km:.2f} km")
    if elapsed:
        minutes, seconds = divmod(elapsed, 60)
        hours, minutes = divmod(minutes, 60)
        lines.append(f"Duration: {hours:02d}:{minutes:02d}:{seconds:02d}")
    if avg_speed > 0:
        pace_s = 1000 / avg_speed  # sec/km
        pace_m, pace_s = divmod(int(pace_s), 60)
        lines.append(f"Avg pace: {pace_m}:{pace_s:02d} /km")
    if elevation:
        lines.append(f"Elevation gain: {elevation:.0f} m")
    if avg_hr:
        lines.append(f"Avg heart rate: {avg_hr:.0f} bpm")
    lines.append(f"Strava: https://www.strava.com/activities/{activity['id']}")

    return {
        "summary": summary,
        "description": "\n".join(lines),
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "source": {
            "title": "Strava",
            "url": f"https://www.strava.com/activities/{activity['id']}",
        },
        "extendedProperties": {
            "private": {"strava_activity_id": str(activity["id"])}
        },
    }


def get_synced_activity_ids(service) -> set[str]:
    """Return the set of Strava activity IDs already on the calendar."""
    synced = set()
    page_token = None
    while True:
        kwargs = {
            "calendarId": CALENDAR_ID,
            "privateExtendedProperty": "strava_activity_id",
            "maxResults": 250,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.events().list(**kwargs).execute()
        for event in result.get("items", []):
            aid = (
                event.get("extendedProperties", {})
                .get("private", {})
                .get("strava_activity_id")
            )
            if aid:
                synced.add(aid)
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return synced


# ---------------------------------------------------------------------------
# Sync state (last sync timestamp)
# ---------------------------------------------------------------------------


def load_sync_state() -> dict:
    if SYNC_STATE_FILE.exists():
        return json.loads(SYNC_STATE_FILE.read_text())
    return {}


def save_sync_state(state: dict):
    SYNC_STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------


def sync(dry_run: bool = False):
    print("Connecting to Google Calendar...")
    service = get_google_calendar_service()

    state = load_sync_state()
    last_sync = state.get("last_sync_timestamp", 0)

    print(f"Fetching Strava activities (after {datetime.fromtimestamp(last_sync) if last_sync else 'all time'})...")
    activities = fetch_strava_activities(after_timestamp=last_sync)
    print(f"Found {len(activities)} new Strava activities.")

    if not activities:
        print("Nothing to sync.")
        return

    print("Checking which activities are already on the calendar...")
    synced_ids = get_synced_activity_ids(service)

    created = 0
    skipped = 0
    for activity in activities:
        aid = str(activity["id"])
        if aid in synced_ids:
            skipped += 1
            continue

        event_body = activity_to_calendar_event(activity)
        if dry_run:
            print(f"  [DRY RUN] Would create: {event_body['summary']}")
        else:
            service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
            print(f"  Created: {event_body['summary']}")
        created += 1

    if not dry_run:
        new_state = {"last_sync_timestamp": int(datetime.now(timezone.utc).timestamp())}
        save_sync_state(new_state)

    print(f"\nDone. Created: {created}, Skipped (already synced): {skipped}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Sync Strava activities to Google Calendar."
    )
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="Run the Strava OAuth2 authorization flow.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without writing to the calendar.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset sync state and re-sync all activities.",
    )
    args = parser.parse_args()

    if args.authorize:
        strava_authorize()
        return

    if args.reset and SYNC_STATE_FILE.exists():
        SYNC_STATE_FILE.unlink()
        print("Sync state reset.")

    sync(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
