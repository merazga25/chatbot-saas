from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse
import os
import json
import requests
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mon_token_secret_123")
PAGE_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")  # doit venir de .env

@app.get("/")
def root():
    return {"ok": True}

# ----- Verification webhook (GET) -----
@app.get("/webhooks/meta")
def webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    print("VERIFY GET:", hub_mode, hub_verify_token, hub_challenge)
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "", status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)

def send_message(psid: str, text: str):
    if not PAGE_TOKEN:
        print("ERROR: PAGE_ACCESS_TOKEN missing in .env")
        return

    url = "https://graph.facebook.com/v19.0/me/messages"
    payload = {
        "recipient": {"id": psid},
        "message": {"text": text},
        "messaging_type": "RESPONSE",
    }
    r = requests.post(url, params={"access_token": PAGE_TOKEN}, json=payload, timeout=15)
    print("SEND:", r.status_code, r.text)

# ----- Events webhook (POST) -----
@app.post("/webhooks/meta")
async def webhook_post(request: Request):
    payload = await request.json()
    print("WEBHOOK POST PAYLOAD:\n", json.dumps(payload, indent=2, ensure_ascii=False))

    # Messenger events: entry -> messaging
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            message_text = event.get("message", {}).get("text")

            # Ignore les messages sans texte (attachments, read receipts, etc.)
            if sender_id and message_text:
                # Réponse simple (on améliorera après en mode vendeur)
                reply = f"Salam 👋 j'ai reçu: {message_text}\nQuel produit tu cherches ?"
                send_message(sender_id, reply)

    return PlainTextResponse("OK", status_code=200)
    @app.get("/debug/supabase")
def debug_supabase():
    res = supabase.table("shops").select("id, name").limit(1).execute()
    return {
        "ok": True,
        "data": res.data
    }

