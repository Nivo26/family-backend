from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
import json
import requests
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from motor.motor_asyncio import AsyncIOMotorClient

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "family_planner")

print("DEBUG ENV_PATH =", ENV_PATH)
print("DEBUG MONGO_URL =", MONGO_URL)
print("DEBUG DB_NAME =", DB_NAME)

app = FastAPI()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://family-frontend-3zbu.onrender.com",
        "https://family-frontend-3zbu.onrender.com/",
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

        start_dt = format_ics_datetime(due, "09:00")
        end_dt = format_ics_datetime(due, "10:00")

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
        raise HTTPException(status_code=500, detail=f"Kunde inte hämta tokens från Google: {response.text}")

    tokens = response.json()
    family_id = state or "family_anders"

    save_tokens_for_family(family_id, tokens)

    return RedirectResponse(url=f"{FRONTEND_URL}?google=connected&family={family_id}")

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

@app.post("/api/planner")
async def save_planner(state: Dict[str, Any], family_id: str = Query("family_default")):
    try:
        planner_id = family_id.strip() or "family_default"

        state_to_save = {
            **state,
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

        return {"ok": True, "updatedAt": state_to_save["updatedAt"]}
    except Exception as e:
        print("POST /api/planner ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()