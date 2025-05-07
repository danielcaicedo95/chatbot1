
# app/services/conversation.py

from datetime import datetime
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

        # 3) Saludo inicial
        if len(user_histories[from_number]) == 1:
            saludo = "¡Hola! 👋 Soy Lucas, tu asistente de Licores El Roble. ¿En qué puedo ayudarte hoy?"
            user_histories[from_number].append({"role": "model", "text": saludo, "time": datetime.utcnow().isoformat()})
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # 4) Obtener catálogo completo con variantes e imágenes
        productos = await get_all_products()
        nombres = [p["name"] for p in productos]

        # Preparar payload completo para LLM multimedia
        catalog_payload = [
            {
                "name": p["name"],
                "images": [img["url"] for img in p.get("product_images", [])],
                "variants": [
                    {
                        "options": v.get("options", {}),
                        "images": [img["url"] for img in v.get("product_images", [])]
                    }
                    for v in p.get("product_variants", [])
                ]
            }
            for p in productos
        ]

        # 5) Multimedia: delegar selección al LLM SIN palabras clave locales
        if re.search(r"\b(foto|imagen)\b", raw_text.lower()):
            # Crear prompt JSON
            prompt_obj = {
                "user_request": raw_text,
                "catalog": catalog_payload,
                "instructions": [
                    "Si el usuario pide imágenes, devuelve JSON EXACTO: {\"send_images\": true, \"images_to_send\": [{\"url\": <url>, \"caption\": <caption>}...]}",
                    "Si no pide imágenes, devuelve {\"send_images\": false}."
                ]
            }
            prompt_text = json.dumps(prompt_obj, ensure_ascii=False)
            llm_input = user_histories[from_number][-10:] + [{"role": "user", "text": prompt_text}]
            llm_resp = await ask_gemini_with_history(llm_input)
            print("🔍 [DEBUG] Raw LLM multimedia response:\n", llm_resp)

            # Parsear JSON de LLM
            action = {"send_images": False}
            try:
                match = re.search(r"\{[\s\S]*\}", llm_resp)
                if match:
                    action = json.loads(match.group())
            except Exception as e:
                print("⚠️ [DEBUG] JSON parse error:", e)

            print("🔍 [DEBUG] Parsed multimedia action:", action)
            if action.get("send_images"):
                images = action.get("images_to_send", []) or []
                if not images:
                    send_whatsapp_message(from_number, "Lo siento, no encontré imágenes para mostrarte.")
                    return
                # Mensaje humano previo
                send_whatsapp_message(from_number, "¡Claro! 😊 Aquí tienes las imágenes:")
                for item in images:
                    url = item.get("url")
                    caption = item.get("caption")
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
                opts = [
                    f"{','.join(f'{k}:{v2}' for k,v2 in var['options'].items())} (stock {var['stock']})"
                    for var in variantes
                ]
                line += f" | Variantes: {', '.join(opts)}"
            imgs = p.get('product_images') or []
            if imgs:
                line += f" | Imágenes: {len(imgs)}"
            contexto_lines.append(line)
        contexto = "Catálogo actual:\n" + "\n".join(contexto_lines)
        print("🔍 [DEBUG] Contexto construido:\n", contexto)

        # 7) Instrucciones para modelo de pedidos
        instrucciones = (
            f"{raw_text}\n\n{contexto}\n\n"
            "INSTRUCCIONES:\n"
            "1. Si producto no disponible sugiere alternativa.\n"
            "2. Si hay intención de compra, detalla cantidad, precios y envío.\n"
            "3. Recomienda producto adicional.\n"
            "4. Si dice 'no', solicita datos de envío.\n"
            "5. Al final incluye JSON exacto en 'order_details'."
        )
        user_histories[from_number].append({"role": "user", "text": instrucciones})
        llm_resp2 = await ask_gemini_with_history(user_histories[from_number])
        print("💬 [DEBUG] LLM order response:\n", llm_resp2)

        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(llm_resp2)
        print("🔍 [DEBUG] order_data:\n", order_data)
        print("🔍 [DEBUG] clean_text:\n", clean_text)

        # 8) Guardar respuesta limpia y enviar
        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)

        # 9) Recomendaciones y cierre
        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
                send_whatsapp_message(from_number, f"🧠 Podrías acompañar tu pedido con:\n{texto_rec}\n¿Te interesa alguno?" )

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