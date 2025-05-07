# app/services/conversation.py

from datetime import datetime, timedelta, timezone
import json
import re
import traceback

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order

# Campos obligatorios para confirmar pedido
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        print("🔍 [DEBUG] Incoming webhook payload:", body)

        # --- 1) Obtener el mensaje del webhook ---
        entry = body["entry"][0]
        changes = entry["changes"][0]
        messages = changes["value"].get("messages")
        if not messages:
            print("⚠️ [DEBUG] No messages in payload")
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        text = raw_text.lower()
        from_number = msg.get("from")
        print(f"🔍 [DEBUG] From: {from_number}, Text: {raw_text}")

        if not raw_text or not from_number:
            print("⚠️ [DEBUG] Missing text or from_number")
            return

        # --- 2) Guardar usuario → historial y Supabase ---
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # --- 3) Primer saludo ---
        if len(user_histories[from_number]) == 1:
            saludo = (
                "¡Hola! 👋 Soy el asistente de *Licores El Roble*.\n"
                "¿Quieres ver nuestro catálogo, resolver alguna duda o hacer un pedido? 🍻"
            )
            user_histories[from_number].append({
                "role": "model",
                "text": saludo,
                "time": datetime.utcnow().isoformat()
            })
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # --- 4) Petición de fotos específicas ---
        if re.search(r"\bfoto(s)?\b|\bimagen(es)?\b", text):
            print("🔍 [DEBUG] Detected image request")
            productos = await get_all_products()
            sent = False
            for p in productos:
                if p["name"].lower() in text:
                    print(f"🔍 [DEBUG] Sending images for product: {p['name']}")
                    for img in p.get("product_images", []):
                        send_whatsapp_image(from_number, img["url"], caption=p["name"])
                        sent = True
            if not sent:
                send_whatsapp_message(from_number, "No encontré imágenes de ese producto.")
            return

        # --- 5) Construir contexto rico con variantes e imágenes ---
        productos = await get_all_products()
        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: COP {p['price']} (stock {p['stock']})"
            variantes = p.get("product_variants") or []
            if variantes:
                opts = ", ".join(
                    f"{','.join(f'{k}:{v}' for k, v in v['options'].items())} (stock {v['stock']})"
                    for v in variantes
                )
                line += f" | Variantes: {opts}"
            imgs = p.get("product_images") or []
            if imgs:
                line += f" | Imágenes: {len(imgs)}"
            contexto_lines.append(line)
        contexto = "Catálogo actual:\n" + "\n".join(contexto_lines)
        print("🔍 [DEBUG] Contexto construido:\n", contexto)

        # --- 6) Instrucciones para el modelo ---
        instrucciones = (
            f"{raw_text}\n\n"
            f"{contexto}\n\n"
            "INSTRUCCIONES:\n"
            "1. Si un producto no está disponible, sugiere alternativa.\n"
            "2. Al ver intención de compra, detalla:\n"
            "   - Productos, cantidad y precio\n"
            "   - Subtotal + COP 5.000 envío\n"
            "   - ¿Deseas algo más?\n"
            "   - Recomienda 1 producto adicional\n"
            "   - Si “no”, pide nombre, dirección, teléfono y pago.\n"
            "3. Usa emojis y tono cercano.\n"
            "4. Al confirmar, al final incluye este JSON EXACTO:\n"
            "{\"order_details\":{\"name\":\"NOMBRE\",\"address\":\"DIRECCIÓN\",\"phone\":\"TELÉFONO\",\"payment_method\":\"TIPO_PAGO\",\"products\":[{\"name\":\"NOMBRE\",\"quantity\":1,\"price\":0}],\"total\":0}}\n"
            "Si el usuario modifica en 5 min, actualiza el pedido.\n"
        )

        # reescribir última entrada del historial con el prompt completo
        user_histories[from_number][-1]["text"] = instrucciones
        gemini_resp = await ask_gemini_with_history(user_histories[from_number])
        print("💬 [DEBUG] Raw LLM response:", gemini_resp)

        # --- 7) Extraer JSON de pedido y limpiar texto ---
        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(gemini_resp)
        print("🔍 [DEBUG] Extracted order_data:", order_data)
        print("🔍 [DEBUG] Clean text:", clean_text)

        # Guardar respuesta limpia
        user_histories[from_number].append({
            "role": "model",
            "text": clean_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", clean_text)

        # --- 8) Recomendaciones si hay productos en el pedido parcial ---
        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            print("🔍 [DEBUG] Recommended products:", recomendaciones)
            if recomendaciones:
                texto_rec = "\n".join(
                    f"- {r['name']}: COP {r['price']}"
                    for r in recomendaciones
                )
                rec_msg = f"🧠 Podrías acompañarlo con:\n{texto_rec}\n¿Te interesa alguno?"
                send_whatsapp_message(from_number, rec_msg)

        # si no hay order_data, envío la respuesta limpia
        if not order_data:
            send_whatsapp_message(from_number, clean_text)

        # --- 9) Procesar la orden si se obtuvo JSON válido ---
        if order_data:
            result = await process_order(from_number, order_data)
            print("🔍 [DEBUG] process_order result:", result)
            status = result.get("status")
            if status == "missing":
                campos = "\n".join(f"- {f.replace('_',' ')}" for f in result.get("fields", []))
                send_whatsapp_message(from_number, f"📋 Faltan datos:\n{campos}")
            elif status == "created":
                send_whatsapp_message(from_number, "✅ Pedido confirmado. ¡Gracias! 🎉")
            elif status == "updated":
                send_whatsapp_message(from_number, "♻️ Pedido actualizado correctamente.")
            else:
                send_whatsapp_message(from_number, "❌ Error guardando el pedido.")

    except Exception as e:
        print("❌ [ERROR] Exception in handle_user_message:\n", traceback.format_exc())
