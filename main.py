# ============================================================
# IMPORTS
# ============================================================
from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse
import os, requests, re
from datetime import datetime, timezone
from supabase import create_client

# ============================================================
# APP
# ============================================================
app = FastAPI()

# ============================================================
# ENV
# ============================================================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mon_token_secret_123")

# Token fallback (si channels.access_token vide)
DEFAULT_PAGE_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# ============================================================
# UTILS
# ============================================================
def now():
    return datetime.now(timezone.utc).isoformat()

def norm(t: str) -> str:
    return (t or "").strip().lower()

def send_message(psid: str, text: str, token: str | None = None):
    """
    Envoie un message à l’utilisateur via Graph API.
    Si token invalide -> logs clairs (code 190/460).
    """
    token = token or DEFAULT_PAGE_TOKEN
    if not token:
        print("❌ PAGE TOKEN MANQUANT (channels.access_token OU PAGE_ACCESS_TOKEN)")
        return

    url = "https://graph.facebook.com/v19.0/me/messages"
    try:
        r = requests.post(
            url,
            params={"access_token": token},
            json={"recipient": {"id": psid}, "message": {"text": text}},
            timeout=10
        )

        if r.status_code >= 400:
            # Essaie de décoder l'erreur Meta
            try:
                payload = r.json()
            except:
                payload = {"raw": r.text}

            print("❌ FB SEND ERROR:", r.status_code, payload)

            # Message utile si token invalide
            err = (payload or {}).get("error") or {}
            if err.get("code") == 190:
                print("⚠️ TOKEN INVALIDÉ (code 190). Regénère un Page Access Token "
                      "et mets-le dans channels.access_token (ou PAGE_ACCESS_TOKEN).")

    except Exception as e:
        print("❌ FB SEND EXCEPTION:", repr(e))

def is_greeting(t):
    t = norm(t)
    return t in ["salam", "slm", "bonjour", "salut", "cc", "saha", "hey", "hi", "السلام", "مرحبا"]

def is_yes(t):
    t = norm(t)
    return t in ["oui", "yes", "ok", "d'accord", "dak", "wah", "نعم", "ايه", "oui.", "yes.", "ok."]

def is_no(t):
    t = norm(t)
    return t in ["non", "no", "nn", "la", "machi", "لا", "nop", "non.", "no."]

def is_cancel(t):
    t = norm(t)
    return t in ["annuler", "cancel", "stop", "khrej", "n7ab ncancel", "nheb ncancel", "إلغاء", "الغاء"]

def parse_quantity(t):
    t = norm(t)
    m = re.search(r"\b(\d+)\b", t)
    if not m:
        return None
    try:
        q = int(m.group(1))
        return q if q > 0 else None
    except:
        return None

def looks_like_price_question(t: str) -> bool:
    t = norm(t)
    keys = ["prix", "price", "combien", "c combien", "cest combien", "بشحال", "شحال", "السعر", "ثمن", "قداش"]
    return any(k in t for k in keys)

def looks_like_order_intent(t: str) -> bool:
    t = norm(t)
    keys = ["nheb", "n7ab", "je veux", "jveux", "commande", "commander", "acheter", "خذ", "خليلي", "بغيت", "نحب", "حاب", "عطيني"]
    return any(k in t for k in keys)

# ============================================================
# DB HELPERS
# ============================================================
def db_required():
    if not supabase:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY manquants")

def get_channel(page_id: str):
    """
    Récupère le channel (boutique) via l'id de page (PAGE_ID).
    external_id = PAGE_ID, platform='messenger'
    """
    db_required()
    if not page_id:
        return None

    res = supabase.table("channels") \
        .select("id,shop_id,access_token") \
        .eq("external_id", page_id) \
        .eq("platform", "messenger") \
        .eq("is_active", True) \
        .limit(1).execute()

    return res.data[0] if res.data else None

def upsert_customer(shop_id, psid):
    db_required()
    if not shop_id or not psid:
        return

    res = supabase.table("customers") \
        .select("id") \
        .eq("shop_id", shop_id) \
        .eq("external_id", psid) \
        .limit(1).execute()

    if res.data:
        supabase.table("customers").update({"last_seen_at": now()}).eq("id", res.data[0]["id"]).execute()
    else:
        supabase.table("customers").insert({
            "shop_id": shop_id,
            "platform": "messenger",
            "external_id": psid,
            "first_seen_at": now(),
            "last_seen_at": now(),
        }).execute()

def get_products(shop_id):
    db_required()
    res = supabase.table("products") \
        .select("id,name,price,stock,keywords") \
        .eq("shop_id", shop_id) \
        .eq("is_active", True).execute()
    return res.data or []

def find_product(shop_id, text):
    """
    Matching:
    1) keywords match
    2) sinon match si nom produit inclus dans text
    """
    text_l = (text or "").lower()
    products = get_products(shop_id)

    # 1) keywords
    for p in products:
        kws = p.get("keywords") or []
        for kw in kws:
            kw_l = (kw or "").lower().strip()
            if kw_l and kw_l in text_l:
                return p

    # 2) name contains (fallback)
    for p in products:
        name_l = (p.get("name") or "").lower().strip()
        if name_l and name_l in text_l:
            return p

    return None

def get_product_by_id(pid):
    db_required()
    if not pid:
        return None
    res = supabase.table("products").select("*").eq("id", pid).limit(1).execute()
    return res.data[0] if res.data else None

def get_active_order(shop_id, psid):
    db_required()
    res = supabase.table("orders") \
        .select("*") \
        .eq("shop_id", shop_id) \
        .eq("customer_psid", psid) \
        .in_("status", ["draft", "awaiting_quantity", "awaiting_confirmation"]) \
        .order("created_at", desc=True) \
        .limit(1).execute()
    return res.data[0] if res.data else None

def create_order(shop_id, channel_id, psid):
    db_required()
    res = supabase.table("orders").insert({
        "shop_id": shop_id,
        "channel_id": channel_id,
        "customer_psid": psid,
        "status": "draft"
    }).execute()
    return res.data[0]["id"] if res.data else None

def set_order_status(order_id, status, extra=None):
    db_required()
    payload = {"status": status}
    if extra:
        payload.update(extra)
    supabase.table("orders").update(payload).eq("id", order_id).execute()

def add_item_no_stock_update(order_id, product, qty):
    """
    On ne déduit PAS le stock ici.
    On déduit à la confirmation.
    """
    db_required()

    stock = int(product.get("stock", 0) or 0)
    if qty > stock:
        return False, "❌ Stock insuffisant"

    unit_price = int(product.get("price", 0) or 0)
    total = qty * unit_price

    # On tente d'insérer avec quantity/line_total.
    # Si ta table order_items n'a pas encore ces colonnes -> fallback.
    payload_full = {
        "order_id": order_id,
        "product_id": product["id"],
        "product_name": product["name"],
        "unit_price": unit_price,
        "quantity": qty,
        "line_total": total
    }
    try:
        supabase.table("order_items").insert(payload_full).execute()
    except Exception as e:
        print("⚠️ order_items insert full failed, fallback:", repr(e))
        payload_min = {
            "order_id": order_id,
            "product_id": product["id"],
            "product_name": product["name"],
            "unit_price": unit_price,
        }
        supabase.table("order_items").insert(payload_min).execute()

    return True, f"✅ {qty} x {product['name']} = {total} DZD\nConfirmer ? (oui / non)"

def confirm_order_and_decrement_stock(order_id):
    """
    Déduit le stock à la confirmation.
    """
    db_required()

    items = supabase.table("order_items").select("product_id,quantity").eq("order_id", order_id).execute().data or []
    if not items:
        return False, "❌ Panier vide."

    # Si ta table order_items n’a pas quantity (fallback), on ne peut pas confirmer proprement.
    # Dans ce cas, on demande au dev d’ajouter la colonne.
    for it in items:
        if it.get("quantity") is None:
            return False, "❌ Table order_items manque la colonne quantity. Ajoute-la (int) pour confirmer les commandes."

    # Vérifier stocks
    for it in items:
        p = get_product_by_id(it["product_id"])
        if not p:
            return False, "❌ Produit introuvable."
        if int(it["quantity"]) > int(p.get("stock", 0) or 0):
            return False, f"❌ Stock insuffisant pour {p.get('name','produit')}."

    # Déduire
    for it in items:
        p = get_product_by_id(it["product_id"])
        new_stock = int(p.get("stock", 0) or 0) - int(it["quantity"])
        supabase.table("products").update({"stock": new_stock}).eq("id", p["id"]).execute()

    set_order_status(order_id, "confirmed")
    return True, "✅ Commande confirmée ! Merci ❤️"

def cancel_order(order_id):
    db_required()
    set_order_status(order_id, "cancelled")
    return "✅ Commande annulée."

# ============================================================
# WEBHOOK VERIFY (GET)
# ============================================================
@app.get("/webhooks/meta")
def verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "")
    return PlainTextResponse("Forbidden", status_code=403)

# ============================================================
# WEBHOOK RECEIVE (POST)
# ============================================================
@app.post("/webhooks/meta")
async def receive(request: Request):
    if not supabase:
        return {"ok": False, "error": "Supabase env manquants"}

    data = await request.json()

    for entry in data.get("entry", []):
        page_id = entry.get("id")
        channel = get_channel(page_id)
        if not channel:
            # Si tu veux debug: print
            print("⚠️ Channel introuvable pour page_id:", page_id)
            continue

        token = channel.get("access_token") or DEFAULT_PAGE_TOKEN

        for ev in entry.get("messaging", []):
            # ignore delivery/read
            if ev.get("delivery") or ev.get("read"):
                continue

            sender = (ev.get("sender") or {}).get("id")
            if not sender:
                continue

            # texte depuis postback ou message
            if (ev.get("postback") or {}).get("payload"):
                text = ev["postback"]["payload"]
            else:
                text = ((ev.get("message") or {}).get("text")) or ""

            text = (text or "").strip()
            if not text:
                send_message(sender, "📩 ابعثلي اسم المنتج بالكتابة من فضلك 😊", token)
                continue

            # customer upsert
            upsert_customer(channel["shop_id"], sender)

            # order state
            order = get_active_order(channel["shop_id"], sender)

            # CANCEL anytime
            if order and is_cancel(text):
                send_message(sender, cancel_order(order["id"]), token)
                continue

            # awaiting_quantity
            if order and order.get("status") == "awaiting_quantity":
                qty = parse_quantity(text)
                if not qty:
                    send_message(sender, "➡️ Envoie un chiffre فقط (1,2,3)", token)
                    continue

                pending_pid = order.get("pending_product_id")
                product = get_product_by_id(pending_pid)
                if not product:
                    set_order_status(order["id"], "draft", {"pending_product_id": None})
                    send_message(sender, "❌ Produit introuvable. قول اسم المنتج من جديد.", token)
                    continue

                ok, msg = add_item_no_stock_update(order["id"], product, qty)
                send_message(sender, msg, token)
                if ok:
                    set_order_status(order["id"], "awaiting_confirmation")
                continue

            # awaiting_confirmation
            if order and order.get("status") == "awaiting_confirmation":
                if is_yes(text):
                    ok, msg = confirm_order_and_decrement_stock(order["id"])
                    send_message(sender, msg, token)
                    continue
                if is_no(text):
                    send_message(sender, cancel_order(order["id"]), token)
                    continue

                send_message(sender, "✅ Confirmer ? Répond: oui / non", token)
                continue

            # greeting
            if is_greeting(text):
                send_message(sender, "👋 Salam ! قول اسم المنتج 😊", token)
                continue

            # product detect
            product = find_product(channel["shop_id"], text)

            # price question
            if looks_like_price_question(text):
                if not product:
                    send_message(sender, "❓ أي منتج تقصد باش نعطيك السعر؟ (مثال: airpods)", token)
                else:
                    send_message(sender, f"💰 {product['name']} = {product['price']} DZD", token)
                continue

            # order intent
            if looks_like_order_intent(text):
                if not product:
                    send_message(sender, "❓ أي منتج تقصد؟ قول الاسم واضح (مثال: airpods)", token)
                    continue

                order_id = create_order(channel["shop_id"], channel["id"], sender)
                if not order_id:
                    send_message(sender, "❌ خطأ في إنشاء الطلب. حاول مرة أخرى.", token)
                    continue

                set_order_status(order_id, "awaiting_quantity", {"pending_product_id": product["id"]})

                # si quantité déjà dans msg
                qty = parse_quantity(text)
                if qty:
                    ok, msg = add_item_no_stock_update(order_id, product, qty)
                    send_message(sender, msg, token)
                    if ok:
                        set_order_status(order_id, "awaiting_confirmation")
                else:
                    send_message(sender, f"🛒 {product['name']} — Quelle quantité ?", token)

                continue

            # product mentioned sans intent -> propose choix
            if product:
                send_message(
                    sender,
                    f"✅ فهمت {product['name']}\nتحب السعر ولا تحب تطلب؟\n- قول: prix {product['name']}\n- أو: nheb {product['name']}",
                    token
                )
                continue

            # fallback
            send_message(sender, "❓ لم أفهم، قول اسم المنتج (مثال: airpods) أو اسأل على السعر (بشحال؟)", token)

    return {"ok": True}

# ============================================================
# ROOT
# ============================================================
@app.get("/")
def root():
    return {"ok": True}
