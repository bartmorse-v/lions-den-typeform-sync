#!/usr/bin/env python3
"""
Typeform → Notion sync for Lion's Den Lead CRM.
Polls for new responses and creates pages in the CRM database.
Run manually or via cron: */5 * * * * /usr/bin/python3 /path/to/typeform_to_notion.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: run  pip3 install requests")

# ── Config ────────────────────────────────────────────────────────────────────
TYPEFORM_TOKEN  = os.getenv("TYPEFORM_TOKEN", "")
NOTION_TOKEN    = os.getenv("NOTION_TOKEN", "")
NOTION_DB_ID    = "813ee0bc-c342-4973-b4ed-4aab288ba7e5"
TYPEFORM_FORM   = "mGWtfzck"
STATE_FILE      = Path(__file__).parent / ".typeform_sync_state.json"

# ── Field IDs → Notion property names ─────────────────────────────────────────
FIELD_MAP = {
    "uUdMYXtgeMAg": "divorce",      # Divorce/Separation?  (single choice)
    "ZRQNtgM6NkAN": "struggles",    # Their Struggles      (multi choice)
    "Oo6uxSwkOaf1": "wife",         # Wife Behaviors       (multi choice)
    "pzXWeoV807Fe": "goals",        # Growth Goals         (multi choice)
    "pLHU8dkF999y": "email",
    "efz4s302c8aM": "first_name",
    "El4kkLsjR7Ym": "last_name",
    "lQEDjRjTeqKM": "phone",
    "OCmD81rAnJpg": "income",
}

# Typeform answer text → Notion option label
DIVORCE_MAP = {
    "Yes": "Yes",
    "No": "No",
    "I'm single": "Not Married Yet",
}

WIFE_MAP = {
    "acts controlling":    "acting controlling",
    "gives me ultimatums": "giving me ultimatums",
    "stonewalls me":       "stonewalling me",
    "is passive aggressive": "being passive aggressive",
    "none of these":       "none of these",
    "acts disrespectful":  None,   # no matching Notion option — skip
}

GOALS_MAP = {
    "act proactive":          "am proactive",
    "stay emotionally grounded": "stay emotionally grounded",
    "listen without reacting":   "listen without reacting",
    "lead the conversation":     "lead the conversation",
    "pursue my passions":        "pursue my passions",
    "connect with other men":    "connect with other men",
    "guard my time":             "guard my time",
    "own my mistakes":           "own my mistakes",
}


# ── State helpers ──────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"synced_ids": []}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Typeform ───────────────────────────────────────────────────────────────────
def fetch_responses(since_token: Optional[str]) -> dict:
    params = {"page_size": 200, "sort": "submitted_at,asc"}
    if since_token:
        params["before"] = None   # handled via synced_ids instead
    headers = {"Authorization": f"Bearer {TYPEFORM_TOKEN}"}
    r = requests.get(
        f"https://api.typeform.com/forms/{TYPEFORM_FORM}/responses",
        headers=headers, params=params, timeout=15
    )
    r.raise_for_status()
    return r.json()


def parse_answer(answer: dict) -> Optional[str]:
    atype = answer.get("type")
    if atype == "choice":
        return answer["choice"].get("label")
    if atype == "choices":
        return answer.get("choices", {}).get("labels", [])
    if atype in ("text", "short_text", "long_text"):
        return answer.get("text")
    if atype == "email":
        return answer.get("email")
    if atype == "phone_number":
        return answer.get("phone_number")
    return None


def extract_fields(response: dict) -> dict:
    data = {}
    for answer in (response.get("answers") or []):
        field_id = answer.get("field", {}).get("id")
        key = FIELD_MAP.get(field_id)
        if key:
            data[key] = parse_answer(answer)
    return data


# ── Notion ─────────────────────────────────────────────────────────────────────
def multi_select(values, mapping=None):
    if not values:
        return []
    result = []
    for v in values:
        label = mapping.get(v, v) if mapping else v
        if label:
            result.append({"name": label})
    return result


def build_notion_payload(fields: dict, submitted_at: str) -> dict:
    first = fields.get("first_name") or ""
    last  = fields.get("last_name") or ""
    name  = f"{first} {last}".strip() or "Unknown"

    properties: dict = {
        "Name":           {"title": [{"text": {"content": name}}]},
        "Stage":          {"select": {"name": "New Lead"}},
        "Response Type":  {"select": {"name": "Completed"}},
        "Submitted":      {"date": {"start": submitted_at}},
    }

    if first:
        properties["First Name"] = {"rich_text": [{"text": {"content": first}}]}
    if last:
        properties["Last Name"] = {"rich_text": [{"text": {"content": last}}]}
    if fields.get("email"):
        properties["Email"] = {"email": fields["email"]}
    if fields.get("phone"):
        properties["Phone"] = {"phone_number": fields["phone"]}
    if fields.get("income"):
        properties["Income Range"] = {"select": {"name": fields["income"]}}
    if fields.get("divorce"):
        mapped = DIVORCE_MAP.get(fields["divorce"], fields["divorce"])
        properties["Divorce/Separation?"] = {"select": {"name": mapped}}

    struggles = multi_select(fields.get("struggles"))
    if struggles:
        properties["Their Struggles"] = {"multi_select": struggles}

    wife = multi_select(fields.get("wife"), WIFE_MAP)
    if wife:
        properties["Wife Behaviors"] = {"multi_select": wife}

    goals = multi_select(fields.get("goals"), GOALS_MAP)
    if goals:
        properties["Growth Goals"] = {"multi_select": goals}

    return {"parent": {"database_id": NOTION_DB_ID}, "properties": properties}


def create_notion_page(payload: dict) -> str:
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=headers,
                      json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not NOTION_TOKEN:
        sys.exit("NOTION_TOKEN is not set. Add it to the script or export it as an env variable.")

    state = load_state()
    synced = set(state.get("synced_ids", []))

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Fetching Typeform responses...")
    data = fetch_responses(None)
    responses = data.get("items", [])
    print(f"  Found {len(responses)} total responses, {len(synced)} already synced.")

    new_count = 0
    for resp in responses:
        resp_id = resp["response_id"]
        if resp_id in synced:
            continue

        fields       = extract_fields(resp)
        submitted_at = resp.get("submitted_at", datetime.now(timezone.utc).isoformat())
        payload      = build_notion_payload(fields, submitted_at)

        try:
            page_id = create_notion_page(payload)
            synced.add(resp_id)
            new_count += 1
            name = f"{fields.get('first_name','')} {fields.get('last_name','')}".strip()
            print(f"  ✓ Created: {name or 'Unknown'} ({resp_id[:8]}…) → Notion {page_id[:8]}…")
        except requests.HTTPError as e:
            print(f"  ✗ Failed for {resp_id}: {e.response.text}")

    state["synced_ids"] = list(synced)
    save_state(state)
    print(f"Done. {new_count} new lead(s) added to Notion.")


if __name__ == "__main__":
    main()
