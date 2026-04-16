from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
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