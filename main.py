# ============================================================
# IMPORTS
# ============================================================
from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse
import os, requests, json, re
from datetime import datetime, timezone
from supabase import create_client

# OpenAI (optionnel)
from openai import OpenAI

# ============================================================
# APP
# ============================================================
app = FastAPI()

# ============================================================
# ENV
# ============================================================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mon_token_secret_123")
DEFAULT_PAGE_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Supabase client (évite crash si env manquants)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ============================================================
# UTILS
# ============================================================
def now():
    return datetime.now(timezone.utc).isoformat()

def send_message(psid, text, token=None):
    token = token or DEFAULT_PAGE_TOKEN
    if not token:
        print("❌ PAGE TOKEN MANQUANT")
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
            print("❌ FB SEND ERROR:", r.status_code, r.text)
    except Exception as e:
        print("❌ FB SEND EXCEPTION:", repr(e))

def norm(t: str) -> str:
    return (t or "").strip().lower()

def is_greeting(t):
    t = norm(t)
    return t in ["salam", "slm", "bonjour", "salut", "cc", "saha", "hey", "hi"]

def is_yes(t):
    t = norm(t)
    return t in ["oui", "yes", "yeah", "y", "ok", "d'accord", "dak", "wah", "ايه", "نعم"]

def is_no(t):
    t = norm(t)
    return t in ["non", "no", "nn", "la", "machi", "لا", "nop"]

def is_cancel(t):
    t = norm(t)
    return t in ["annuler", "cancel", "stop", "khrej", "n7ab ncancel", "nheb ncancel", "إلغاء", "الغاء"]

def parse_quantity(t):
    t = norm(t)
    # accepte "2" ou "x2" ou "qty 2"
    m = re.search(r"\b(\d+)\b", t)
    if not m:
        return None
    try:
        q = int(m.group(1))
        return q if q > 0 else None
    except:
        return None

# ============================================================
# DB HELPERS
# ============================================================
def db_required():
    if not supabase:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY manquants")

def get_channel(page_id):
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
        supabase.table("customers").update({"last_seen_at": now()}) \
            .eq("id", res.data[0]["id"]).execute()
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
    # matching simple par keywords
    text_l = (text or "").lower()
    for p in get_products(shop_id):
        kws = p.get("keywords") or []
        for kw in kws:
            if (kw or "").lower() in text_l:
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
    IMPORTANT: on ne déduit PAS le stock ici.
    On déduit le stock seulement quand le client confirme.
    """
    db_required()
    if qty > int(product.get("stock", 0)):
        return False, "❌ Stock insuffisant"

    unit_price = int(product["price"])
    total = qty * unit_price

    supabase.table("order_items").insert({
        "order_id": order_id,
        "product_id": product["id"],
        "product_name": product["name"],
        "unit_price": unit_price,
        "quantity": qty,
        "line_total": total
    }).execute()

    return True, f"✅ {qty} x {product['name']} = {total} DZD\nConfirmer ? (oui / non)"

def confirm_order_and_decrement_stock(order_id):
    """
    Déduit le stock à la confirmation (simple version).
    NOTE: sans transaction, en prod tu feras une RPC/transaction SQL.
    """
    db_required()

    items = supabase.table("order_items").select("product_id,quantity").eq("order_id", order_id).execute().data or []
    if not items:
        return False, "❌ Panier vide."

    # Vérifier stocks
    for it in items:
        p = get_product_by_id(it["product_id"])
        if not p:
            return False, "❌ Produit introuvable."
        if int(it["quantity"]) > int(p.get("stock", 0)):
            return False, f"❌ Stock insuffisant pour {p.get('name','produit')}."

    # Déduire
    for it in items:
        p = get_product_by_id(it["product_id"])
        new_stock = int(p.get("stock", 0)) - int(it["quantity"])
        supabase.table("products").update({"stock": new_stock}).eq("id", p["id"]).execute()

    set_order_status(order_id, "confirmed")
    return True, "✅ Commande confirmée ! Merci ❤️"

def cancel_order(order_id):
    db_required()
    set_order_status(order_id, "cancelled")
    return "✅ Commande annulée."

# ============================================================
# OPENAI (INTENT EXTRACTOR)
# ============================================================
LLM_PROMPT = """
Tu es un extracteur d'intention pour un chatbot e-commerce en Algérie (Darija/Français/Arabe).
Répond UNIQUEMENT en JSON valide.

Format:
{
 "intent": "greeting|ask_price|place_order|unknown",
 "product": null,
 "quantity": null
}

Règles:
- "place_order" si le client veut acheter/commander ("nheb", "je veux", "commande", "خليلي", "بغيت"...)
- "ask_price" si il demande le prix ("prix", "combien", "بشحال")
- product: renvoie le nom du produit si واضح, sinon null
- quantity: renvoie un entier si واضح, sinon null
"""

def llm_classify(text):
    if not oai:
        return {"intent": "unknown", "product": None, "quantity": None}

    try:
        # Si ton SDK/model supporte response_format json_object, c’est le meilleur.
        r = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LLM_PROMPT},
                {"role": "user", "content": text},
            ]
        )
        obj = json.loads(r.choices[0].message.content)
        # normalisation minimal
        if "intent" not in obj:
            obj["intent"] = "unknown"
        obj.setdefault("product", None)
        obj.setdefault("quantity", None)
        return obj
    except Exception as e:
        # fallback ultra-safe
        print("[LLM ERROR]", repr(e))
        return {"intent": "unknown", "product": None, "quantity": None}

# ============================================================
# WEBHOOK VERIFY
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
# WEBHOOK RECEIVE
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
            continue

        token = channel.get("access_token") or DEFAULT_PAGE_TOKEN

        for e in entry.get("messaging", []):
            # Ignore delivery/read events
            if e.get("delivery") or e.get("read"):
                continue

            sender = (e.get("sender") or {}).get("id")
            if not sender:
                continue

            # Postback (boutons) -> treat as text payload
            if e.get("postback", {}).get("payload"):
                text = e["postback"]["payload"]
            else:
                text = (e.get("message") or {}).get("text") or ""

            text = text.strip()
            if not text:
                # pas de texte (image, audio, sticker...)
                send_message(sender, "📩 ابعثلي اسم المنتج بالكتابة من فضلك 😊", token)
                continue

            upsert_customer(channel["shop_id"], sender)

            # =========================
            # CANCEL (à tout moment)
            # =========================
            order = get_active_order(channel["shop_id"], sender)
            if order and is_cancel(text):
                send_message(sender, cancel_order(order["id"]), token)
                continue

            # =========================
            # STATE: awaiting_quantity
            # =========================
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

            # =========================
            # STATE: awaiting_confirmation
            # =========================
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

            # =========================
            # RULES: greeting
            # =========================
            if is_greeting(text):
                send_message(sender, "👋 Salam ! قول اسم المنتج 😊", token)
                continue

            # =========================
            # LLM: intent
            # =========================
            intent = llm_classify(text)

            # Place order
            if intent.get("intent") == "place_order":
                # Essayez produit depuis intent.product sinon text
                product = find_product(channel["shop_id"], intent.get("product") or text)
                if not product:
                    send_message(sender, "❓ أي منتج تقصد؟ (قول الاسم واضح)", token)
                    continue

                order_id = create_order(channel["shop_id"], channel["id"], sender)
                if not order_id:
                    send_message(sender, "❌ خطأ في إنشاء الطلب. حاول مرة أخرى.", token)
                    continue

                set_order_status(order_id, "awaiting_quantity", {"pending_product_id": product["id"]})

                # si quantity déjà détectée par LLM, on peut sauter السؤال
                qty = intent.get("quantity")
                if isinstance(qty, int) and qty > 0:
                    ok, msg = add_item_no_stock_update(order_id, product, qty)
                    send_message(sender, msg, token)
                    if ok:
                        set_order_status(order_id, "awaiting_confirmation")
                else:
                    send_message(sender, f"🛒 {product['name']} — Quelle quantité ?", token)

                continue

            # Ask price (optionnel: réponse بسيطة)
            if intent.get("intent") == "ask_price":
                product = find_product(channel["shop_id"], intent.get("product") or text)
                if not product:
                    send_message(sender, "❓ أي منتج تقصد باش نعطيك السعر؟", token)
                else:
                    send_message(sender, f"💰 {product['name']} = {product['price']} DZD", token)
                continue

            # =========================
            # FALLBACK
            # =========================
            send_message(sender, "❓ لم أفهم، قول اسم المنتج (مثال: airpods)", token)

    return {"ok": True}

# ============================================================
# DEBUG
# ============================================================
@app.get("/")
def root():
    return {"ok": True}

@app.get("/debug/llm")
def debug_llm(q: str = "nheb 2 airpods"):
    return llm_classify(q)
