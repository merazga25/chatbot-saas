if active_order and active_order.get("status") == "awaiting_quantity":
    # ✅ sortie / reset si client veut annuler
    t = (text or "").strip().lower()
    if t in ["annuler", "cancel", "stop", "khrej", "n7ab ncancel", "no", "non"]:
        try:
            supabase.table("orders").update({
                "status": "cancelled",
                "pending_product_id": None,
                "pending_product_name": None,
            }).eq("id", active_order["id"]).execute()
        except Exception as e:
            print("[CANCEL ERROR]", repr(e))
        send_message(sender_id, "❌ Ok, j’ai annulé. Dis-moi quel produit tu veux 😊", page_token)
        continue

    # ✅ si le client dit salam pendant attente quantité
    if is_greeting(text):
        send_message(sender_id, "👋 Salam ! Rani نستنى غير الكمية بالأرقام (مثال: 1 ولا 2 ولا 3).", page_token)
        continue

    qty = parse_quantity(text)
    if qty is None:
        send_message(
            sender_id,
            "➡️ Envoie juste la quantité en chiffre (ex: 1, 2, 3...) ou écris (annuler).",
            page_token
        )
        continue

    product_id = active_order.get("pending_product_id")
    if not product_id:
        send_message(sender_id, "⚠️ Produit manquant. Dis-moi le produit à commander.", page_token)
        try:
            supabase.table("orders").update({"status": "draft"}).eq("id", active_order["id"]).execute()
        except Exception as e:
            print("[ORDER RESET ERROR]", repr(e))
        continue

    product = load_product(shop_id, product_id)
    if not product:
        send_message(sender_id, "⚠️ Produit introuvable. Dis-moi le produit à commander.", page_token)
        try:
            supabase.table("orders").update({"status": "draft"}).eq("id", active_order["id"]).execute()
        except Exception as e:
            print("[ORDER RESET ERROR]", repr(e))
        continue

    # ✅ debug utile
    print("QTY FLOW:", {"order_id": active_order["id"], "product_id": product_id, "qty": qty})

    try:
        ok, reply = create_order_item_and_decrease_stock(shop_id, active_order["id"], product, qty)
        send_message(sender_id, reply, page_token)

        if ok:
            supabase.table("orders").update({"status": "awaiting_confirmation"}).eq("id", active_order["id"]).execute()

    except Exception as e:
        print("[QTY FLOW ERROR]", repr(e))
        send_message(
            sender_id,
            "⚠️ Erreur DB pendant l’enregistrement.\n"
            "Vérifie: order_items.quantity existe + orders.pending_product_id existe + SERVICE_ROLE_KEY.\n"
            "Puis réessaie.",
            page_token
        )

    continue
