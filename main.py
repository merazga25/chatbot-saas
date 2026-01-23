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
# 2) VARIABLES D’ENVIRONNEMENT (Railway Variables)
# ============================================================
# Vérification webhook Meta (GET /webhooks/meta)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mon_token_secret_123")

# Token de ta Page Facebook (pour envoyer des messages via Graph API)
PAGE_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

# Supabase (URL + clé serveur)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Création du client Supabase (sert à lire/écrire dans la DB)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# 3) FONCTIONS UTILITAIRES (HELPERS)
# ============================================================

def now_utc_iso() -> str:
    """Retourne la date/heure actuelle en UTC au format ISO (texte)."""
    return datetime.now(timezone.utc).isoformat()


def send_message(psid: str, text: str) -> dict:
    """
    Envoie un message à un utilisateur Messenger via Graph API.
    - psid = sender_id (id du client Messenger)
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
    """
    Détecte si le message est une salutation.
    """
    t = (text or "").strip().lower()
    greetings = ["salam", "slm", "salut", "bonjour", "bonsoir", "cc", "coucou", "saha"]
    return t in greetings or any(t.startswith(g + " ") for g in greetings)


def greeting_reply() -> str:
    """Message de réponse pour une salutation."""
    return "Salam 👋 Marhba bik! قولي اسم المنتج ولا واش حبيت تشري 😊"


def wants_to_buy(text: str) -> bool:
    """
    Détecte l’intention d’achat.
    On commence avec des mots-clés (simple).
    """
    t = (text or "").lower().strip()

    buy_words = [
        # FR
        "acheter", "achète", "achat",
        "commande", "commander",
        "je veux", "je veux acheter", "je veux commander",

        # Darija (différentes écritures)
        "n7ab", "n7eb", "nheb",
        "nhab", "nheb nchri", "nhab nchri",
        "chri", "nchri", "nchra",

        # “commander” écrit différemment
        "ncommander", "ncommandi", "ncommand", "ncommande",
    ]

    return any(w in t for w in buy_words)


def resolve_shop_id(page_id: str) -> str | None:
    """
    Multi-tenant :
    On récupère shop_id à partir de la Page Facebook (entry.id).
    Table: channels
      - platform='messenger'
      - external_id = page_id
      - is_active = true
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
    Enregistre le client s’il n’existe pas encore,
    sinon met à jour last_seen_at.
    Table: customers
      - shop_id, platform, external_id
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

    # Si le client existe -> update last_seen_at
    if existing:
        supabase.table("customers").update({
            "last_seen_at": now_utc_iso()
        }).eq("id", existing[0]["id"]).execute()
        return

    # Sinon -> créer nouveau client
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
    Table: products (keywords = jsonb array)
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
    Crée (ou récupère) une commande 'draft' pour ce client et cette boutique.
    Table: orders
      - shop_id
      - customer_psid
      - status = 'draft'
    IMPORTANT: on n'utilise PAS la colonne platform ici (car chez toi elle n'existe pas).
    """
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

    created = (
        supabase.table("orders")
        .insert({
            "shop_id": shop_id,
            "customer_psid": psid,
            "status": "draft",
            "created_at": now_utc_iso(),
            "updated_at": now_utc_iso(),
        })
        .execute()
    )

    return created.data[0]["id"]


# ============================================================
# 4) ROUTES DE TEST (debug)
# ============================================================

@app.get("/")
def root():
    return {"ok": True}


@app.get("/debug/env")
def debug_env():
    """
    Vérifie si les variables importantes existent dans Railway.
    (True/False seulement)
    """
    return {
        "VERIFY_TOKEN": bool(os.getenv("VERIFY_TOKEN")),
        "PAGE_ACCESS_TOKEN": bool(os.getenv("PAGE_ACCESS_TOKEN")),
        "SUPABASE_URL": bool(os.getenv("SUPABASE_URL")),
        "SUPABASE_SERVICE_ROLE_KEY": bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
    }


@app.get("/debug/supabase")
def debug_supabase():
    """Teste un select simple sur la table shops."""
    res = supabase.table("shops").select("id, name").limit(1).execute()
    return {"ok": True, "data": res.data}


# ============================================================
# 5) WEBHOOK META (GET) -> Vérification
# ============================================================

@app.get("/webhooks/meta")
def webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """
    Meta appelle ce endpoint pour vérifier ton webhook.
    Tu dois renvoyer hub.challenge si le token est bon.
    """
    print("VERIFY GET:", hub_mode, hub_verify_token, hub_challenge)

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "", status_code=200)

    return PlainTextResponse("Forbidden", status_code=403)


# ============================================================
# 6) WEBHOOK META (POST) -> Réception des messages
# ============================================================

@app.post("/webhooks/meta")
async def webhook_receive(request: Request):
    """
    Meta envoie ici les messages Messenger.
    On doit répondre 200 rapidement.
    """
    payload = await request.json()

    entries = payload.get("entry") or []
    for entry in entries:
        page_id = entry.get("id")
        shop_id = resolve_shop_id(page_id)

        print("PAGE_ID:", page_id, "shop_id:", shop_id)

        for event in (entry.get("messaging") or []):
            sender_id = (event.get("sender") or {}).get("id")
            if not sender_id:
                continue

            # On ne traite que les messages texte
            msg = event.get("message") or {}
            text = msg.get("text") or ""
            if not text:
                continue

            # Si la page n'est pas liée à une boutique
            if not shop_id:
                send_message(sender_id, "⚠️ Page non reliée à une boutique (channels).")
                continue

            # 1) Enregistrer le client (même salam)
            upsert_customer(shop_id, "messenger", sender_id)

            # 2) Salutation -> réponse directe sans DB produits
            if is_greeting(text):
                send_message(sender_id, greeting_reply())
                continue

            # 3) Chercher produit dans la boutique
            product = find_product_by_text(shop_id, text)

            # 4) Si le client veut commander mais ne donne aucun produit
            if not product and wants_to_buy(text):
                send_message(sender_id, "🛒 D'accord ! Quel produit veux-tu commander ? (ex: airpods)")
                continue

            # 5) Produit trouvé -> soit info, soit création commande draft
            if product:
                if wants_to_buy(text):
                    order_id = get_or_create_draft_order(shop_id, sender_id)
                    send_message(sender_id, f"🧾 Commande créée (brouillon)\nID: {order_id}\n➡️ Quelle quantité ?")
                    continue

                # Info produit (prix/stock)
                name = product["name"]
                price = product["price"]
                stock = product["stock"]
                send_message(sender_id, f"📦 {name}\n💰 Prix: {price} DZD\n✅ Stock: {stock}")
                continue

            # 6) Sinon -> produit introuvable
            send_message(sender_id, "Je n’ai pas trouvé ce produit 😅\nEssaie un nom plus clair.")

    return {"ok": True}
