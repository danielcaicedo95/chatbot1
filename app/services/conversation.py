from datetime import datetime
import json
import re
import traceback
from difflib import get_close_matches

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
        # 1) Depurar payload completo
        print("🔍 [DEBUG] Incoming webhook payload:\n", json.dumps(body, indent=2, ensure_ascii=False))
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages")
        if not messages:
            print("⚠️ [DEBUG] No messages in payload")
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        print(f"🔍 [DEBUG] From: {from_number}, Text: '{raw_text}'")
        if not raw_text or not from_number:
            print("⚠️ [DEBUG] Missing text or from_number")
            return

        # 2) Guardar en historial y Supabase
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # 3) Saludo inicial si es primera interacción
        if len(user_histories[from_number]) == 1:
            saludo = "¡Hola! 👋 Soy tu asistente de Licores El Roble. ¿En qué puedo ayudarte hoy?"
            user_histories[from_number].append({"role": "model", "text": saludo, "time": datetime.utcnow().isoformat()})
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # 4) Obtener catálogo completo con variantes e imágenes
        productos = await get_all_products()

        # 5) Detectar petición de imágenes sin LLM
        if re.search(r"\b(foto|imagen)s?\b", raw_text, re.IGNORECASE):
            # Buscamos coincidencia en nombre de producto o variante
            choice = None
            for p in productos:
                if p["name"].lower() in raw_text.lower():
                    choice = (p, None)
                    break
                for v in p.get("product_variants", []):
                    opts = [str(val).lower() for val in v.get("options", {}).values()]
                    if any(opt in raw_text.lower() for opt in opts):
                        choice = (p, v)
                        break
                if choice:
                    break
            # Si no hay match exacto, fuzzy
            if not choice:
                names = [p["name"] for p in productos]
                match = get_close_matches(raw_text, names, n=1, cutoff=0.5)
                if match:
                    prod = next(p for p in productos if p["name"] == match[0])
                    choice = (prod, None)
            # Si aún no hay match
            if not choice:
                send_whatsapp_message(from_number, "No encontré ese producto. ¿Puedes revisar el nombre? 😕")
                return

            prod, var = choice
            # Recopilar URLs
            urls = []
            caption = prod["name"] if not var else ",".join(f"{k}:{v}" for k,v in var.get("options", {}).items())
            if var:
                for img in var.get("product_images", []):
                    urls.append(img.get("url"))
            else:
                for img in prod.get("product_images", []):
                    urls.append(img.get("url"))
            # filtrar formatos
            urls = [u for u in urls if u and u.lower().endswith((".png",".jpg",".jpeg"))]
            if not urls:
                send_whatsapp_message(from_number, "Lo siento, no hay imágenes compatibles para mostrar. 😔")
                return
            send_whatsapp_message(from_number, f"¡Claro! 😊 Aquí tienes las fotos de {caption}:")
            for url in urls:
                try:
                    send_whatsapp_image(from_number, url, caption=caption)
                except Exception as e:
                    print(f"❌ [ERROR] sending image {url}: {e}")
                    send_whatsapp_message(from_number, f"No pude enviar la imagen de {caption}.")
            return

        # 6) Flujo de texto/pedidos: construir contexto rico
        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: COP {p['price']} (stock {p['stock']})"
            variantes = p.get('product_variants') or []
            if variantes:
                opts = []
                for v in variantes:
                    opts.append(
                        f"{','.join(f'{k}:{v2}' for k,v2 in v['options'].items())} (stock {v['stock']})"
                    )
                line += f" | Variantes: {', '.join(opts)}"
            imgs = p.get('product_images') or []
            if imgs:
                line += f" | Imágenes: {len(imgs)}"
            contexto_lines.append(line)
        contexto = "Catálogo actual:\n" + "\n".join(contexto_lines)
        print("🔍 [DEBUG] Contexto construido:\n", contexto)

        # 7) Instrucciones al LLM para pedidos (incluye payment_method)
        instrucciones = (
            f"{raw_text}\n\n{contexto}\n\n"
            "INSTRUCCIONES:\n"
            "1. Si un producto no está disponible, sugiere alternativa.\n"
            "2. Si hay intención de compra, detalla productos, cantidad, precio + COP 5.000 envío.\n"
            "3. Recomienda 1 producto adicional.\n"
            "4. Si el usuario dice 'no', pide nombre, dirección, teléfono y método de pago.\n"
            "5. Al confirmar, al final incluye este JSON EXACTO:\n"
            "{\"order_details\":{\"name\":\"NOMBRE\",\"address\":\"DIRECCIÓN\",\"phone\":\"TELÉFONO\",\"payment_method\":\"TIPO_PAGO\",\"products\":[{\"name\":\"NOMBRE\",\"quantity\":1,\"price\":0}],\"total\":0}}"
        )
        user_histories[from_number].append({"role": "user", "text": instrucciones})
        llm_resp = await ask_gemini_with_history(user_histories[from_number])
        print("💬 [DEBUG] LLM order flow response:\n", llm_resp)

        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(llm_resp)
        print("🔍 [DEBUG] order_data:\n", order_data)
        print("🔍 [DEBUG] clean_text:\n", clean_text)

        # 8) Guardar respuesta limpia y enviar
        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)

        # 9) Recomendaciones y procesar pedido
        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
                send_whatsapp_message(from_number, f"🧠 Podrías acompañar tu pedido con:\n{texto_rec}\n¿Te interesa alguno?")

        if not order_data:
            send_whatsapp_message(from_number, clean_text)
        else:
            result = await process_order(from_number, order_data)
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

    except Exception:
        print("❌ [ERROR] in handle_user_message:\n", traceback.format_exc())
