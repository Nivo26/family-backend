from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from motor.motor_asyncio import AsyncIOMotorClient

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "family_planner")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

print("DEBUG ENV_PATH =", ENV_PATH)
print("DEBUG DB_NAME =", DB_NAME)
print("DEBUG HAS_GOOGLE_CLIENT_ID =", bool(GOOGLE_CLIENT_ID))
print("DEBUG HAS_GOOGLE_REDIRECT_URI =", bool(GOOGLE_REDIRECT_URI))

app = FastAPI()

# OBS:
# Detta fungerar för test, men på Render är lokal fil-lagring inte en bra långsiktig lösning.
# Senare bör Google-tokens sparas i MongoDB i stället.
TOKENS_FILE = Path("google_tokens.json")


def load_google_tokens():
    if not TOKENS_FILE.exists():
        return {}
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_google_tokens(data):
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def save_tokens_for_family(family_id: str, tokens: dict):
    all_tokens = load_google_tokens()
    all_tokens[family_id] = tokens
    save_google_tokens(all_tokens)


def get_tokens_for_family(family_id: str):
    all_tokens = load_google_tokens()
    return all_tokens.get(family_id)


def refresh_google_access_token(refresh_token: str):
    token_url = "https://oauth2.googleapis.com/token"

    token_data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    response = requests.post(token_url, data=token_data)

    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Kunde inte förnya Google access token: {response.text}",
        )

    return response.json()


def create_google_calendar_event(family_id: str, task: dict):
    saved_tokens = get_tokens_for_family(family_id)

    if not saved_tokens:
        raise HTTPException(status_code=400, detail="Ingen Google-koppling finns för detta hushåll")

    refresh_token = saved_tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Ingen refresh token sparad för detta hushåll")

    fresh_token_data = refresh_google_access_token(refresh_token)
    access_token = fresh_token_data.get("access_token")

    if not access_token:
        raise HTTPException(status_code=500, detail="Google gav ingen access token")

    if not task.get("due") or not task.get("dueTime"):
        raise HTTPException(status_code=400, detail="Tasken måste ha datum och tid")

    start_datetime = f"{task['due']}T{task['dueTime']}:00"
    start_dt = datetime.fromisoformat(start_datetime)
    end_dt = start_dt + timedelta(minutes=30)

    reminder_minutes = task.get("reminderMinutes")
    reminders = {"useDefault": False, "overrides": []}

    if reminder_minutes != "" and reminder_minutes is not None:
        reminders["overrides"].append({
            "method": "popup",
            "minutes": int(reminder_minutes),
        })

    event_body = {
        "summary": task.get("title", "Uppgift"),
        "description": task.get("note", ""),
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": "Europe/Stockholm",
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": "Europe/Stockholm",
        },
        "reminders": reminders,
    }

    response = requests.post(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=event_body,
    )

    if response.status_code not in [200, 201]:
        raise HTTPException(status_code=500, detail=f"Kunde inte skapa Google-event: {response.text}")

    return response.json()

def sync_tasks_to_google(family_id: str, tasks: list):
    synced_tasks = []

    for task in tasks:
        task_copy = dict(task)

        sync_enabled = task_copy.get("syncEnabled", True)
        has_datetime = bool(task_copy.get("due")) and bool(task_copy.get("dueTime"))
        already_has_google_event = bool(task_copy.get("googleEventId"))

        if sync_enabled and has_datetime and not already_has_google_event:
            try:
                event = create_google_calendar_event(family_id, task_copy)
                task_copy["googleEventId"] = event.get("id", "")
            except Exception as e:
                print("GOOGLE SYNC TASK ERROR:", task_copy.get("title"), str(e))

        synced_tasks.append(task_copy)

    return synced_tasks


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://family-frontend-3zbu.onrender.com",
        "https://family-frontend-3zbu.onrender.com/",
        "https://mighty-tasks.emergent.host",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]
collection = db["planner"]

DEFAULT_STATE: Dict[str, Any] = {
    "planner_id": "",
    "members": [
        {"id": "m1", "name": "Anders"},
        {"id": "m2", "name": "Familjen"},
    ],
    "currentTab": "biz",
    "selectedDate": "2026-04-14",
    "tabs": [
        {
            "id": "biz",
            "label": "Företaget",
            "color": "#378ADD",
            "icon": "briefcase",
            "locked": False,
            "ownerId": "m1",
            "isShared": False,
            "sharedWith": [],
        },
        {
            "id": "pastor",
            "label": "Pastor",
            "color": "#7F77DD",
            "icon": "church",
            "locked": False,
            "ownerId": "m1",
            "isShared": False,
            "sharedWith": [],
        },
        {
            "id": "family",
            "label": "Familj",
            "color": "#1D9E75",
            "icon": "home",
            "locked": False,
            "ownerId": "m1",
            "isShared": True,
            "sharedWith": ["m2"],
        },
        {
            "id": "prayer",
            "label": "Bön",
            "color": "#7F77DD",
            "icon": "heart",
            "locked": True,
            "ownerId": "m1",
            "isShared": True,
            "sharedWith": ["m2"],
        },
    ],
    "tasks": [],
    "prayers": [],
}


async def get_or_create_planner(family_id: str) -> Dict[str, Any]:
    planner_id = family_id.strip() or "family_default"

    doc = await collection.find_one({"planner_id": planner_id}, {"_id": 0})
    if doc:
        return doc

    initial_doc = {
        **DEFAULT_STATE,
        "planner_id": planner_id,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    await collection.insert_one(initial_doc)
    return initial_doc


def format_ics_datetime(date_str: str, time_str: str = "09:00") -> str:
    hour, minute = time_str.split(":")
    return f"{date_str.replace('-', '')}T{hour}{minute}00"


def escape_ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def build_ics(planner: Dict[str, Any]) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Familjeplanerare//SV//",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_ics_text(planner.get('planner_id', 'Familjeplanerare'))}",
        "X-WR-TIMEZONE:Europe/Stockholm",
    ]

    tabs_by_id = {tab["id"]: tab for tab in planner.get("tabs", [])}

    for task in planner.get("tasks", []):
        due = task.get("due")
        if not due:
            continue

        tab = tabs_by_id.get(task.get("area"))
        tab_label = tab.get("label", "Planering") if tab else "Planering"
        owner_id = tab.get("ownerId") if tab else None
        owner_name = next(
            (member["name"] for member in planner.get("members", []) if member["id"] == owner_id),
            "Okänd",
        )

        event_time = task.get("dueTime") or "09:00"
        start_dt = format_ics_datetime(due, event_time)

        try:
            start_native = datetime.fromisoformat(f"{due}T{event_time}:00")
        except ValueError:
            start_native = datetime.fromisoformat(f"{due}T09:00:00")

        end_native = start_native + timedelta(minutes=30)
        end_dt = end_native.strftime("%Y%m%dT%H%M%S")

        summary = escape_ics_text(task.get("title", "Uppgift"))
        description_parts = [
            f"Flik: {tab_label}",
            f"Ägare: {owner_name}",
            f"Status: {task.get('status', 'todo')}",
        ]
        if task.get("note"):
            description_parts.append(task["note"])

        description = escape_ics_text("\n".join(description_parts))

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{task.get('id', uuid4().hex)}@familjeplanerare",
            f"DTSTAMP:{now_utc}",
            f"DTSTART:{start_dt}",
            f"DTEND:{end_dt}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ])

    for prayer in planner.get("prayers", []):
        uid = prayer.get("id", uuid4().hex)
        summary = escape_ics_text(f"Bön: {prayer.get('title', 'Bön')}")
        description = escape_ics_text("Böneämne från Familjeplanerare")

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}@familjeplanerare",
            f"DTSTAMP:{now_utc}",
            "DTSTART;VALUE=DATE:20260414",
            "DTEND;VALUE=DATE:20260415",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


@app.get("/")
async def root():
    return {"status": "ok", "message": "Familjeplanerare API körs"}


@app.get("/test123")
def test123():
    return {
        "ok": True,
        "has_google_client_id": bool(GOOGLE_CLIENT_ID),
        "has_google_redirect_uri": bool(GOOGLE_REDIRECT_URI),
        "frontend_url": FRONTEND_URL,
    }


@app.get("/google/test-tokens")
def google_test_tokens(family_id: str = "family_anders"):
    all_tokens = load_google_tokens()
    return {
        "family_id": family_id,
        "all_saved_family_ids": list(all_tokens.keys()),
        "has_tokens_for_family": bool(all_tokens.get(family_id)),
        "token_keys": list(all_tokens.get(family_id, {}).keys()),
    }


@app.get("/google/start")
def google_start(family_id: str = "family_anders"):
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Google OAuth är inte konfigurerat i backend")

    google_auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=https://www.googleapis.com/auth/calendar"
        f"&access_type=offline"
        f"&prompt=consent"
        f"&state={family_id}"
    )

    return RedirectResponse(url=google_auth_url)


@app.get("/google/callback")
def google_callback(code: str = None, state: str = "family_anders"):
    try:
        if not code:
            raise HTTPException(status_code=400, detail="Ingen code från Google")

        token_url = "https://oauth2.googleapis.com/token"

        token_data = {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        }

        response = requests.post(token_url, data=token_data)

        if response.status_code != 200:
            raise HTTPException(
                status_code=500,
                detail=f"Kunde inte hämta tokens från Google: {response.text}",
            )

        tokens = response.json()
        family_id = state or "family_anders"

        print("GOOGLE CALLBACK FAMILY ID:", family_id)
        print("GOOGLE CALLBACK TOKEN KEYS:", list(tokens.keys()))

        save_tokens_for_family(family_id, tokens)

        saved = get_tokens_for_family(family_id)
        print("GOOGLE CALLBACK SAVED OK:", bool(saved))

        return RedirectResponse(url=f"{FRONTEND_URL}?google=connected&family={family_id}")

    except Exception as e:
        print("GOOGLE CALLBACK ERROR:", str(e))
        raise HTTPException(status_code=500, detail=f"Google callback kraschade: {str(e)}")


@app.get("/google/test-create-event")
def google_test_create_event(family_id: str = "family_anders"):
    test_task = {
        "title": "Test från planner",
        "note": "Detta är ett testevent från appens backend",
        "due": "2026-04-18",
        "dueTime": "14:00",
        "reminderMinutes": 10,
    }

    event = create_google_calendar_event(family_id, test_task)

    return {
        "ok": True,
        "google_event_id": event.get("id"),
        "google_event_link": event.get("htmlLink"),
    }


@app.get("/api/planner.ics")
async def planner_ics_feed(family_id: str = Query("family_default")):
    try:
        planner = await get_or_create_planner(family_id)
        ics_content = build_ics(planner)

        return Response(
            content=ics_content,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as e:
        print("GET /api/planner.ics ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/planner")
async def get_planner(family_id: str = Query("family_default")):
    try:
        return await get_or_create_planner(family_id)
    except Exception as e:
        print("GET /api/planner ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/planner")
async def save_planner(state: Dict[str, Any], family_id: str = Query("family_default")):
    try:
        planner_id = family_id.strip() or "family_default"

        incoming_tasks = state.get("tasks", [])
        synced_tasks = sync_tasks_to_google(planner_id, incoming_tasks)

        state_to_save = {
            **state,
            "tasks": synced_tasks,
            "planner_id": planner_id,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }

        result = await collection.update_one(
            {"planner_id": planner_id},
            {"$set": state_to_save},
            upsert=True,
        )

        if not result.acknowledged:
            raise HTTPException(status_code=500, detail="Kunde inte spara till databasen")

        return {
            "ok": True,
            "updatedAt": state_to_save["updatedAt"],
            "tasks": synced_tasks,
        }
    except Exception as e:
        print("POST /api/planner ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
