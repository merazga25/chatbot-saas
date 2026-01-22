from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse
import os
import requests

from supabase import create_client

app = FastAPI()

# =========================
# ENV / TOKENS
# =========================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mon_token_secret_123")

# Token global pour la Page (au début). Plus tard: token par boutique via channels.access_token
PAGE_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Création client Supabase (backend)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# =========================
# HELPERS
# =========================
def send_message(psid: str, text: str) -> dict:
    """
    Envoie un message à l'utilisateur Messenger via Graph API.
    psid = sender_id
    """
    if not PAGE_TOKEN:
        print("[ERROR] PAGE_ACCESS_TOKEN missing in env")
        return {"ok": False, "error": "PAGE_ACCESS_TOKEN missing"}

    url = "https://graph.facebook.com/v19.0/me/messages"
    payload = {
        "recipient": {"id": psid},
        "message": {"text": text},
    }
    params = {"access_token": PAGE_TOKEN}

    r = requests.post(url, params=params, json=payload, timeout=15)
    print("SEND:", r.status_code, r.text)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "status_code": r.status_code, "raw": r.text}


def resolve_shop_id(page_id: str) -> str | None:
    """
    Résout shop_id à partir du PAGE_ID Messenger (entry.id) en utilisant channels.
    channels: platform='messenger' AND external_id=page_id AND is_active=true
    """
    res = (
        supabase.table("channels")
        .select("shop_id")
        .eq("platform", "messenger")
        .eq("external_id", page_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    data = res.data or []
    if not data:
        return None
    return data[0]["shop_id"]


# =========================
# BASIC ROUTES
# =========================
@app.get("/")
def root():
    return {"ok": True}


@app.get("/debug/ping")
def debug_ping():
    return {"ping": "pong"}


@app.get("/debug/env")
def debug_env():
    return {
        "VERIFY_TOKEN": bool(os.getenv("VERIFY_TOKEN")),
        "PAGE_ACCESS_TOKEN": bool(os.getenv("PAGE_ACCESS_TOKEN")),
        "SUPABASE_URL": bool(os.getenv("SUPABASE_URL")),
        "SUPABASE_SERVICE_ROLE_KEY": bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
    }


@app.get("/debug/supabase")
def debug_supabase():
    """
    Vérifie que Supabase répond (table shops).
    """
    res = supabase.table("shops").select("id, name").limit(1).execute()
    return {"ok": True, "data": res.data}


# =========================
# META WEBHOOK VERIFY (GET)
# =========================
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


# =========================
# META WEBHOOK RECEIVE (POST)
# =========================
@app.post("/webhooks/meta")
async def webhook_receive(request: Request):
    payload = await request.json()
    # print("WEBHOOK POST PAYLOAD:", payload)  # décommente si tu veux tout voir

    # Meta envoie object="page"
    entries = payload.get("entry", [])

    for entry in entries:
        page_id = entry.get("id")  # <-- ID de la Page Facebook
        if not page_id:
            continue

        # Résoudre shop_id via channels
        shop_id = resolve_shop_id(page_id)

        messaging_events = entry.get("messaging", [])
        for event in messaging_events:
            sender_id = (event.get("sender") or {}).get("id")  # PSID client
            if not sender_id:
                continue

            # message texte
            msg = event.get("message") or {}
            text = msg.get("text") or ""

            # postback (boutons)
            postback = event.get("postback") or {}
            payload_postback = postback.get("payload")

            if not shop_id:
                # Boutique pas configurée (channels manquant)
                # (tu peux aussi juste ignorer)
                reply = "⚠️ Cette page n’est pas encore reliée à une boutique (channels)."
                send_message(sender_id, reply)
                continue

            # Exemple réponse tenant-aware
            if payload_postback:
                reply = f"✅ shop_id={shop_id}\nPostback: {payload_postback}"
            else:
                reply = f"✅ shop_id={shop_id}\nTu as dit: {text}"

            send_message(sender_id, reply)

    return {"ok": True}
