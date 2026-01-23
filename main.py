from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse
import os
import requests
import re
from datetime import datetime, timezone
from supabase import create_client

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mon_token_secret_123")
DEFAULT_PAGE_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# -------------------------
# Helpers
# -------------------------

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_channel_by_page_id(page_id: str) -> dict | None:
    if not page_id:
        return None
    res = (
        supabase.table("channels")
        .select("id,shop_id,access_token,is_active")
        .eq("platform", "messenger")
        .eq("external_id", page_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None

def send_message(psid: str, text: str, page_token: str | None = None) -> dict:
    token = page_token or DEFAULT_PAGE_TOKEN
    if not token:
        print("[ERROR] PAGE_ACCESS_TOKEN missing (env + channels.access_token)")
        return {"ok": False, "error": "PAGE_ACCESS_TOKEN missing"}

    url = "https://graph.facebook.com/v19.0/me/messages"
    payload = {"recipient": {"id": psid}, "message": {"text": text}}
    params = {"access_token": token}

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

def wants_to_buy(text: str) -> bool:
    t = (text or "").lower().strip()
    buy_words = [
        "acheter", "achète", "achat",
        "commande", "commander",
        "je veux", "je veux acheter", "je veux commander",
        "n7ab", "n7eb", "nheb",
        "nhab", "nheb nchri", "nhab nchri",
        "chri", "nchri", "nchra",
        "ncommander", "ncommande", "ncommand", "ncommandi",
    ]
    return any(w in t for w in buy_words)

def parse_quantity(text: str) -> int | None:
    t = (text or "").strip()
    if re.fullmatch(r"\d+", t):
        q = int(t)
        return q if q > 0 else None
    return None

def upsert_customer(shop_id: str, platform: str, psid: str) -> None:
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
    q = (text or "").lower()

    res = (
        supabase.table("products")
        .select("id,name,price,stock,keywords,is_active")
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

def get_or_create_draft_order(shop_id: str, channel_id: str, psid: str) -> str:
    res = (
        supabase.table("orders")
        .select("id")
        .eq("shop_id", shop_id)
        .eq("customer_psid", psid)
        .eq("status", "draft")
        .order("created_at", desc=True)
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
            "channel_id": channel_id,
            "customer_psid": psid,
            "status": "draft",
        })
        .execute()
    )
    return created.data[0]["id"]

def get_active_order(shop_id: str, psid: str) -> dict | None:
    res = (
        supabase.table("orders")
        .select("id,status,pending_product_id,pending_product_name,created_at")
        .eq("shop_id", shop_id)
        .eq("customer_psid", psid)
        .in_("status", ["awaiting_quantity", "awaiting_confirmation", "draft"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None

def set_order_awaiting_quantity(order_id: str, product: dict) -> None:
    supabase.table("orders").update({
        "status": "awaiting_quantity",
        "pending_product_id": product["id"],
        "pending_product_name": product["name"],
    }).eq("id", order_id).execute()

def load_product(shop_id: str, product_id: str) -> dict | None:
    res = (
        supabase.table("products")
        .select("id,name,price,stock,is_active")
        .eq("shop_id", shop_id)
        .eq("id", product_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None

def create_order_item_and_decrease_stock(shop_id: str, order_id: str, product: dict, quantity: int) -> tuple[bool, str]:
    name = product["name"]
    price = int(product["price"])
    stock = int(product["stock"])

    if quantity > stock:
        return False, f"⚠️ Stock insuffisant pour {name}. Disponible: {stock}."

    supabase.table("order_items").insert({
        "order_id": order_id,
        "product_id": product["id"],
        "product_name": name,
        "unit_price": price,
        "quantity": quantity,
    }).execute()

    new_stock = stock - quantity
    supabase.table("products").update({
        "stock": new_stock
    }).eq("id", product["id"]).eq("shop_id", shop_id).execute()

    total = quantity * price
    return True, (
        f"✅ {quantity} x {name}\n"
        f"💰 Prix unitaire: {price} DZD\n"
        f"💵 Total: {total} DZD\n"
        f"📦 Stock restant: {new_stock}\n\n"
        f"Confirmer la commande ? (oui / non)"
    )

def is_yes(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ["oui", "ok", "okay", "d'accord", "daccord", "confirm", "confirmer"]

def is_no(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ["non", "annuler", "annule", "cancel", "stop", "no"]


# -------------------------
# Routes debug
# -------------------------

@app.get("/")
def root():
    return {"ok": True}

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
    res = supabase.table("shops").select("id,name").limit(1).execute()
    return {"ok": True, "data": res.data}


# -------------------------
# Webhook verify
# -------------------------

@app.get("/webhooks/meta")
def webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "", status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)


# -------------------------
# Webhook receive
# -------------------------

@app.post("/webhooks/meta")
async def webhook_receive(request: Request):
    try:
        payload = await request.json()
    except Exception as e:
        print("[ERROR] invalid json:", repr(e))
        return {"ok": True}

    entries = payload.get("entry") or []
    for entry in entries:
        page_id = entry.get("id")

        try:
            channel = get_channel_by_page_id(page_id)
        except Exception as e:
            print("[CHANNEL ERROR]", repr(e))
            channel = None

        if not channel:
            print("PAGE_ID:", page_id, "=> channel not found")
        else:
            print("PAGE_ID:", page_id, "shop_id:", channel["shop_id"], "channel_id:", channel["id"])

        for event in (entry.get("messaging") or []):
            try:
                sender_id = (event.get("sender") or {}).get("id")
                if not sender_id:
                    continue

                msg = event.get("message") or {}
                if msg.get("is_echo"):
                    continue

                text = msg.get("text") or ""
                if not text:
                    continue

                # Pas de boutique liée
                if not channel:
                    send_message(sender_id, "⚠️ Page non reliée à une boutique (channels).")
                    continue

                shop_id = channel["shop_id"]
                channel_id = channel["id"]
                page_token = channel.get("access_token") or DEFAULT_PAGE_TOKEN

                # Upsert customer
                try:
                    upsert_customer(shop_id, "messenger", sender_id)
                except Exception as e:
                    print("[CUSTOMER ERROR]", repr(e))

                # --------- STATE ROUTER ---------
                try:
                    active_order = get_active_order(shop_id, sender_id)
                except Exception as e:
                    print("[ACTIVE ORDER ERROR]", repr(e))
                    active_order = None

                # awaiting_quantity
                if active_order and active_order.get("status") == "awaiting_quantity":
                    qty = parse_quantity(text)
                    if qty is None:
                        send_message(sender_id, "➡️ Envoie juste la quantité en chiffre (ex: 1, 2, 3...)", page_token)
                        continue

                    product_id = active_order.get("pending_product_id")
                    if not product_id:
                        send_message(sender_id, "⚠️ Produit manquant. Dis-moi le produit à commander.", page_token)
                        supabase.table("orders").update({"status": "draft"}).eq("id", active_order["id"]).execute()
                        continue

                    product = load_product(shop_id, product_id)
                    if not product:
                        send_message(sender_id, "⚠️ Produit introuvable. Dis-moi le produit à commander.", page_token)
                        supabase.table("orders").update({"status": "draft"}).eq("id", active_order["id"]).execute()
                        continue

                    ok, reply = create_order_item_and_decrease_stock(shop_id, active_order["id"], product, qty)
                    send_message(sender_id, reply, page_token)

                    if ok:
                        supabase.table("orders").update({"status": "awaiting_confirmation"}).eq("id", active_order["id"]).execute()
                    continue

                # awaiting_confirmation
                if active_order and active_order.get("status") == "awaiting_confirmation":
                    if is_yes(text):
                        supabase.table("orders").update({
                            "status": "confirmed",
                            "pending_product_id": None,
                            "pending_product_name": None,
                        }).eq("id", active_order["id"]).execute()
                        send_message(sender_id, "✅ Commande confirmée ! Merci 😊", page_token)
                        continue

                    if is_no(text):
                        supabase.table("orders").update({
                            "status": "cancelled",
                            "pending_product_id": None,
                            "pending_product_name": None,
                        }).eq("id", active_order["id"]).execute()
                        send_message(sender_id, "❌ Commande annulée. Si tu veux autre chose dis-moi 😊", page_token)
                        continue

                    send_message(sender_id, "Confirmer ? Réponds par (oui / non).", page_token)
                    continue

                # --------- NORMAL FLOW ---------
                if is_greeting(text):
                    send_message(sender_id, greeting_reply(), page_token)
                    continue

                product = find_product_by_text(shop_id, text)

                if not product and wants_to_buy(text):
                    send_message(sender_id, "🛒 D'accord ! Quel produit veux-tu commander ? (ex: airpods)", page_token)
                    continue

                if product:
                    if wants_to_buy(text):
                        order_id = get_or_create_draft_order(shop_id, channel_id, sender_id)
                        set_order_awaiting_quantity(order_id, product)
                        send_message(sender_id, f"🧾 D'accord pour: {product['name']}\n➡️ Quelle quantité ? (ex: 1, 2, 3...)", page_token)
                        continue

                    send_message(sender_id, f"📦 {product['name']}\n💰 Prix: {product['price']} DZD\n✅ Stock: {product['stock']}", page_token)
                    continue

                send_message(sender_id, "Je n’ai pas trouvé ce produit 😅\nEssaie un nom plus clair.", page_token)

            except Exception as e:
                print("[EVENT ERROR]", repr(e))
                continue

    return {"ok": True}
