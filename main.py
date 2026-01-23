from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse
import os
import requests
from datetime import datetime, timezone
from supabase import create_client

# ============================================================
# 1) APP FASTAPI
# ============================================================
app = FastAPI()

# ============================================================
# 2) VARIABLES D’ENV (Railway / GitHub Secrets)
# ============================================================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mon_token_secret_123")
PAGE_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Client Supabase
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# 3) HELPERS (fonctions)
# ============================================================

def now_utc_iso() -> str:
    """Date/heure UTC au format ISO."""
    return datetime.now(timezone.utc).isoformat()


def send_message(psid: str, text: str) -> dict:
    """
    Envoie un message à un utilisateur Messenger via Graph API.
    psid = sender_id
    """
    if not PAGE_TOKEN:
        print("[ERROR] PAGE_ACCESS_TOKEN missing in env")
        return {"ok": False, "error": "PAGE_ACCESS_TOKEN missing"}

    url = "https://graph.facebook.com/v19.0/me/messages"
    payload = {"recipient": {"id": psid}, "message": {"text": text}}
    params = {"access_token": PAGE_TOKEN}

    r = requests.post(url, params=params, json=payload, timeout=15)
    print("SEND:", r.status_code, r.text)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "status_code": r.status_code, "raw": r.text}


def is_greeting(text: str) -> bool:
    """Détecte si le message est une salutation."""
    t = (text or "").strip().lower()
    greetings = ["salam", "slm", "salut", "bonjour", "bonsoir", "cc", "coucou", "saha"]
    return t in greetings or any(t.startswith(g + " ") for g in greetings)


def greeting_reply() -> str:
    """Réponse quand le client dit salam/bonjour."""
    return "Salam 👋 Marhba bik! قولي اسم المنتج ولا واش حبيت تشري 😊"


def wants_to_buy(text: str) -> bool:
    """
    Détecte l’intention d’achat (règles simples).
    On couvre plusieurs écritures: nhab/n7ab, ncommander, etc.
    """
    t = (text or "").lower().strip()

    buy_words = [
        # FR
        "acheter", "achète", "achat",
        "commande", "commander",
        "je veux", "je veux acheter", "je veux commander",

        # Darija (variantes)
        "n7ab", "n7eb", "nheb",
        "nhab", "nheb nchri", "nhab nchri",
        "chri", "nchri", "nchra",

        # “commander” écrit différemment
        "ncommander", "ncommande", "ncommand", "ncommandi",
    ]

    return any(w in t for w in buy_words)


def resolve_shop_id(page_id: str) -> str | None:
    """
    Multi-tenant:
    on récupère shop_id via channels en utilisant la Page ID (entry.id).
    channels(platform='messenger', external_id=page_id, is_active=true)
    """
    if not page_id:
        return None

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


def upsert_customer(shop_id: str, platform: str, psid: str) -> None:
    """
    Enregistre le client (customers) si n’existe pas,
    sinon update last_seen_at.
    """
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

    if existing:
        supabase.table("customers").update({
            "last_seen_at": now_utc_iso()
        }).eq("id", existing[0]["id"]).execute()
        return

    supabase.table("customers").insert({
        "shop_id": shop_id,
        "platform": platform,
        "external_id": psid,
        "first_seen_at": now_utc_iso(),
        "last_seen_at": now_utc_iso(),
    }).execute()


def find_product_by_text(shop_id: str, text: str) -> dict | None:
    """
    Cherche un produit de la boutique dont un keyword apparaît dans le texte.
    products.keywords = jsonb array
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
            if kw and str(kw).lower() in q:
                return p
    return None


def get_or_create_draft_order(shop_id: str, psid: str) -> str:
    """
    IMPORTANT: ta table orders n’a PAS la colonne platform.
    Donc on ne la filtre PAS.
    On crée/récupère une commande draft pour (shop_id + customer_psid).
    """
    # 1) Chercher draft existante
    res = (
        supabase.table("orders")
        .select("id")
        .eq("shop_id", shop_id)
        .eq("customer_psid", psid)
        .eq("status", "draft")
        .limit(1)
        .execute()
    )
    data = res.data or []
    if data:
        return data[0]["id"]

    # 2) Sinon créer
    created = (
        supabase.table("orders")
        .insert({
            "shop_id": shop_id,
            "customer_psid": psid,
            "status": "draft",
        })
        .execute()
    )

    return created.data[0]["id"]


# ============================================================
# 4) ROUTES BASIC / DEBUG
# ============================================================

@app.get("/")
def root():
    return {"ok": True}


@app.get("/debug/env")
def debug_env():
    """Vérifie si les variables sont présentes."""
    return {
        "VERIFY_TOKEN": bool(os.getenv("VERIFY_TOKEN")),
        "PAGE_ACCESS_TOKEN": bool(os.getenv("PAGE_ACCESS_TOKEN")),
        "SUPABASE_URL": bool(os.getenv("SUPABASE_URL")),
        "SUPABASE_SERVICE_ROLE_KEY": bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
    }


@app.get("/debug/supabase")
def debug_supabase():
    """Teste Supabase avec un select simple."""
    res = supabase.table("shops").select("id,name").limit(1).execute()
    return {"ok": True, "data": res.data}


# ============================================================
# 5) WEBHOOK META VERIFY (GET)
# ============================================================

@app.get("/webhooks/meta")
def webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "", status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)


# ============================================================
# 6) WEBHOOK META RECEIVE (POST)
# ============================================================

@app.post("/webhooks/meta")
async def webhook_receive(request: Request):
    """
    Reçoit les messages Messenger.
    IMPORTANT: on met try/except pour éviter "silence" en cas d’erreur DB.
    """
    try:
        payload = await request.json()
    except Exception as e:
        print("[ERROR] invalid json:", repr(e))
        return {"ok": True}

    entries = payload.get("entry") or []
    for entry in entries:
        page_id = entry.get("id")
        shop_id = resolve_shop_id(page_id)
        print("PAGE_ID:", page_id, "shop_id:", shop_id)

        for event in (entry.get("messaging") or []):
            sender_id = (event.get("sender") or {}).get("id")
            if not sender_id:
                continue

            msg = event.get("message") or {}
            text = msg.get("text") or ""
            if not text:
                continue

            print("INCOMING:", {"sender_id": sender_id, "text": text})

            # Si pas de boutique liée
            if not shop_id:
                send_message(sender_id, "⚠️ Page non reliée à une boutique (channels).")
                continue

            # Toujours enregistrer le client
            try:
                upsert_customer(shop_id, "messenger", sender_id)
            except Exception as e:
                print("[CUSTOMER ERROR]", repr(e))

            # Salutation
            if is_greeting(text):
                send_message(sender_id, greeting_reply())
                continue

            # Chercher produit
            product = None
            try:
                product = find_product_by_text(shop_id, text)
            except Exception as e:
                print("[PRODUCT SEARCH ERROR]", repr(e))
                send_message(sender_id, "⚠️ Erreur recherche produit. Réessaie.")
                continue

            # Si client veut commander mais pas de produit dans le message
            if not product and wants_to_buy(text):
                send_message(sender_id, "🛒 D'accord ! Quel produit veux-tu commander ? (ex: airpods)")
                continue

            # Produit trouvé
            if product:
                # Si intention achat -> créer commande draft
                if wants_to_buy(text):
                    try:
                        order_id = get_or_create_draft_order(shop_id, sender_id)
                        send_message(sender_id, f"🧾 Commande créée (brouillon)\nID: {order_id}\n➡️ Quelle quantité ?")
                    except Exception as e:
                        print("[ORDER ERROR]", repr(e))
                        send_message(sender_id, "⚠️ Erreur création commande. Réessaie.")
                    continue

                # Sinon -> info produit
                name = product["name"]
                price = product["price"]
                stock = product["stock"]
                send_message(sender_id, f"📦 {name}\n💰 Prix: {price} DZD\n✅ Stock: {stock}")
                continue

            # Produit introuvable
            send_message(sender_id, "Je n’ai pas trouvé ce produit 😅\nEssaie un nom plus clair.")

    return {"ok": True}
