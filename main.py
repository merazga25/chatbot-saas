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

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
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
    requests.post(
        url,
        params={"access_token": token},
        json={"recipient": {"id": psid}, "message": {"text": text}},
        timeout=10
    )

def is_greeting(t):
    t = (t or "").lower().strip()
    return t in ["salam", "slm", "bonjour", "salut", "cc", "saha"]

def parse_quantity(t):
    t = (t or "").strip()
    return int(t) if re.fullmatch(r"\d+", t) else None

# ============================================================
# DB HELPERS
# ============================================================
def get_channel(page_id):
    res = supabase.table("channels") \
        .select("id,shop_id,access_token") \
        .eq("external_id", page_id) \
        .eq("platform", "messenger") \
        .eq("is_active", True) \
        .limit(1).execute()
    return res.data[0] if res.data else None

def upsert_customer(shop_id, psid):
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

def find_product(shop_id, text):
    res = supabase.table("products") \
        .select("id,name,price,stock,keywords") \
        .eq("shop_id", shop_id) \
        .eq("is_active", True).execute()
    for p in res.data or []:
        for kw in p.get("keywords") or []:
            if kw.lower() in text.lower():
                return p
    return None

def get_active_order(shop_id, psid):
    res = supabase.table("orders") \
        .select("*") \
        .eq("shop_id", shop_id) \
        .eq("customer_psid", psid) \
        .in_("status", ["draft", "awaiting_quantity", "awaiting_confirmation"]) \
        .order("created_at", desc=True) \
        .limit(1).execute()
    return res.data[0] if res.data else None

def create_order(shop_id, channel_id, psid):
    res = supabase.table("orders").insert({
        "shop_id": shop_id,
        "channel_id": channel_id,
        "customer_psid": psid,
        "status": "draft"
    }).execute()
    return res.data[0]["id"]

def add_item(order_id, product, qty, shop_id):
    if qty > product["stock"]:
        return False, "❌ Stock insuffisant"
    total = qty * product["price"]

    supabase.table("order_items").insert({
        "order_id": order_id,
        "product_id": product["id"],
        "product_name": product["name"],
        "unit_price": product["price"],
        "quantity": qty,
        "line_total": total
    }).execute()

    supabase.table("products").update({
        "stock": product["stock"] - qty
    }).eq("id", product["id"]).execute()

    return True, f"✅ {qty} x {product['name']} = {total} DZD\nConfirmer ? (oui / non)"

# ============================================================
# OPENAI (INTENT EXTRACTOR)
# ============================================================
LLM_PROMPT = """
Tu es un extracteur d'intention.
Répond UNIQUEMENT en JSON.

Format:
{
 "intent": "greeting|ask_price|place_order|unknown",
 "product": null,
 "quantity": null
}
"""

def llm_classify(text):
    if not oai:
        return {"intent": "unknown", "product": None, "quantity": None}
    try:
        r = oai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": LLM_PROMPT},
                {"role": "user", "content": text},
            ]
        )
        return json.loads(r.choices[0].message.content)
    except:
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
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("Forbidden", status_code=403)

# ============================================================
# WEBHOOK RECEIVE
# ============================================================
@app.post("/webhooks/meta")
async def receive(request: Request):
    data = await request.json()
    for entry in data.get("entry", []):
        channel = get_channel(entry.get("id"))
        if not channel:
            continue

        for e in entry.get("messaging", []):
            psid = e["sender"]["id"]
            text = e.get("message", {}).get("text", "")
            if not text:
                continue

            upsert_customer(channel["shop_id"], psid)

            # STATE
            order = get_active_order(channel["shop_id"], psid)
            if order and order["status"] == "awaiting_quantity":
                qty = parse_quantity(text)
                if not qty:
                    send_message(psid, "➡️ Envoie un chiffre فقط (1,2,3)", channel["access_token"])
                    continue
                product = supabase.table("products").select("*") \
                    .eq("id", order["pending_product_id"]).execute().data[0]
                ok, msg = add_item(order["id"], product, qty, channel["shop_id"])
                send_message(psid, msg, channel["access_token"])
                if ok:
                    supabase.table("orders").update({
                        "status": "awaiting_confirmation"
                    }).eq("id", order["id"]).execute()
                continue

            # RULES
            if is_greeting(text):
                send_message(psid, "👋 Salam ! قول اسم المنتج 😊", channel["access_token"])
                continue

            # LLM
            intent = llm_classify(text)

            if intent["intent"] == "place_order":
                product = find_product(channel["shop_id"], intent.get("product") or text)
                if not product:
                    send_message(psid, "❓ أي منتج تقصد؟", channel["access_token"])
                    continue
                order_id = create_order(channel["shop_id"], channel["id"], psid)
                supabase.table("orders").update({
                    "status": "awaiting_quantity",
                    "pending_product_id": product["id"]
                }).eq("id", order_id).execute()
                send_message(psid, f"🛒 {product['name']} — Quelle quantité ?", channel["access_token"])
                continue

            # FALLBACK
            send_message(psid, "❓ لم أفهم، قول اسم المنتج", channel["access_token"])

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
