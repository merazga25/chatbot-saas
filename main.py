from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse
import os
import requests
from datetime import datetime, timezone
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

def is_greeting(text: str) -> bool:
    t = (text or "").strip().lower()
    greetings = ["salam", "slm", "salut", "bonjour", "bonsoir", "cc", "coucou", "saha"]
    return t in greetings or any(t.startswith(g + " ") for g in greetings)

def greeting_reply() -> str:
    return "Salam 👋 Marhba bik! قولي اسم المنتج ولا واش حبيت تشري 😊"

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

def find_product_by_text(shop_id: str, text: str):
    """
    Cherche un produit de la boutique dont un keyword apparaît dans le texte.
    Simple v1 (on améliorera après).
    """
    q = (text or "").lower()

    res = (
        supabase.table("products")
        .select("id,name,price,stock,keywords")
        .eq("shop_id", shop_id)
        .eq("is_active", True)
        .execute()
    )
    products = res.data or []

    for p in products:
        kws = p.get("keywords") or []
        for kw in kws:
            if kw and kw.lower() in q:
                return p
    return None
def upsert_customer(shop_id: str, platform: str, psid: str) -> None:
    """
    Crée le client s’il n’existe pas (shop_id + platform + psid),
    sinon met à jour last_seen_at.
    """

    # 1) Chercher si le client existe déjà
    res = (
        supabase.table("customers")
        .select("id")
        .eq("shop_id", shop_id)
        .eq("platform", platform)
        .eq("external_id", psid)
        .limit(1)
        .execute()
    )

    existing = res.data or []

    # 2) S'il existe → update last_seen_at
    if existing:
        supabase.table("customers").update({
            "last_seen_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", existing[0]["id"]).execute()
        return

    # 3) Sinon → insert nouveau client
    supabase.table("customers").insert({
        "shop_id": shop_id,
        "platform": platform,
        "external_id": psid,
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    def get_or_create_draft_order(shop_id: str, psid: str) -> str:
    """
    Retourne l'id d'une commande draft existante,
    ou en crée une nouvelle si elle n'existe pas.
    """

    # 1) Chercher une commande draft existante
    res = (
        supabase.table("orders")
        .select("id")
        .eq("shop_id", shop_id)
        .eq("platform", "messenger")
        .eq("customer_psid", psid)
        .eq("status", "draft")
        .limit(1)
        .execute()
    )

    data = res.data or []

    if data:
        return data[0]["id"]

    # 2) Sinon, créer une nouvelle commande draft
    created = (
        supabase.table("orders")
        .insert({
            "shop_id": shop_id,
            "platform": "messenger",
            "customer_psid": psid,
            "status": "draft",
        })
        .execute()
    )

    return created.data[0]["id"]
    def wants_to_buy(text: str) -> bool:
    """
    Retourne True si le message contient une intention d'achat.
    """
    t = (text or "").lower()

    buy_words = [
        "acheter", "achète", "achat",
        "commande", "commander",
        "n7ab ncommader", "je veux commander", "n7eb ", "commande"
        "chri", "nchri", "nchra",
        "prends", "prendre"
    ]

    return any(word in t for word in buy_words)


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

    entries = payload.get("entry") or []
    for entry in entries:
        page_id = entry.get("id")
        if not page_id:
            continue

        shop_id = resolve_shop_id(page_id)
        print("PAGE_ID:", page_id, "shop_id:", shop_id)

        for event in (entry.get("messaging") or []):
            sender_id = (event.get("sender") or {}).get("id")
            if not sender_id:
                continue

            # Message texte (ignorer read/delivery/etc.)
            msg = event.get("message") or {}
            text = msg.get("text") or ""
            if not text:
                continue

            # Si pas de boutique liée
            if not shop_id:
                send_message(sender_id, "⚠️ Page non reliée à une boutique (channels).")
                continue

            # ✅ Toujours enregistrer/mettre à jour le client (même salam)
            upsert_customer(shop_id, "messenger", sender_id)

            # ✅ Salutation
            if is_greeting(text):
                send_message(sender_id, greeting_reply())
                continue

            # 🔎 Chercher produit (dans la boutique)
            product = find_product_by_text(shop_id, text)

            if product:
                # ✅ Si le client veut acheter/commander → créer (ou récupérer) une commande draft
                if wants_to_buy(text):
                    order_id = get_or_create_draft_order(shop_id, sender_id)
                    send_message(
                        sender_id,
                        f"🧾 Commande créée (brouillon)\nID: {order_id}\n➡️ Quelle quantité ?"
                    )
                    continue

                # Sinon: info produit
                name = product["name"]
                price = product["price"]
                stock = product["stock"]
                send_message(sender_id, f"📦 {name}\n💰 Prix: {price} DZD\n✅ Stock: {stock}")
            else:
                send_message(sender_id, f"Je n’ai pas trouvé ce produit 😅\nEssaie un nom plus clair.")

    return {"ok": True}
