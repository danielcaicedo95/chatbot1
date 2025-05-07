
# app/services/conversation.py

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
        # 1) Depurar payload
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

        # 2) Guardar historial
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)
        print("🔍 [DEBUG] User message saved to Supabase")

        # 3) Saludo inicial
        if len(user_histories[from_number]) == 1:
            saludo = "¡Hola! 👋 Soy Lucas, tu asistente de Licores El Roble. ¿En qué puedo ayudarte hoy?"
            user_histories[from_number].append({"role": "model", "text": saludo, "time": datetime.utcnow().isoformat()})
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # 4) Detección petición de imágenes
        productos = await get_all_products()
        nombres = [p["name"] for p in productos]

        prompt = (
            "Eres un asistente que detecta si el usuario quiere ver imágenes de un producto.\n"
            f"Catálogo: {', '.join(nombres)}.\n"
            f"Mensaje: '{raw_text}'.\n"
            "Responde con un JSON válido sin formateo Markdown, ejemplo:"
            "{'send_images': true, 'product_name': 'Vodka Absolut'}"
            "o {'send_images': false}."
        )
        llm_input = user_histories[from_number][-10:] + [{"role": "user", "text": prompt}]
        llm_resp = await ask_gemini_with_history(llm_input)
        print("🔍 [DEBUG] Raw LLM image intent response:\n", llm_resp)

        # 4.1) Limpiar JSON de respuesta
        json_match = re.search(r"\{.*\}", llm_resp, re.DOTALL)
        action = {"send_images": False}
        if json_match:
            try:
                action = json.loads(json_match.group())
            except Exception as e:
                print(f"⚠️ [DEBUG] JSON parse error: {e}")
        else:
            print("⚠️ [DEBUG] No JSON object found in LLM response.")
        print("🔍 [DEBUG] Parsed action:", action)

        if action.get("send_images"):
            prod_name = action.get("product_name", "").strip()
            if prod_name not in nombres:
                matches = get_close_matches(prod_name, nombres, n=1, cutoff=0.6)
                if matches:
                    prod_name = matches[0]
                    print(f"🔍 [DEBUG] Fuzzy matched to '{prod_name}'")
                else:
                    send_whatsapp_message(from_number, f"No encontré '{prod_name}'. ¿Puedes verificar el nombre?")
                    return

            send_whatsapp_message(from_number, f"¡Claro! 😊 Buscando imágenes de *{prod_name}*...")
            producto = next((p for p in productos if p["name"] == prod_name), None)
            imgs = producto.get("product_images", []) if producto else []

            if not imgs:
                send_whatsapp_message(from_number, f"No tenemos imágenes de *{prod_name}* en este momento.")
                return
            for img in imgs:
                url = img.get("url")
                try:
                    send_whatsapp_image(from_number, url, caption=prod_name)
                except Exception as e:
                    print(f"❌ [ERROR] sending image {url}: {e}")
                    send_whatsapp_message(from_number, f"Ocurrió un error enviando una imagen de {prod_name}.")
            return

        # 5) Flujo normal de texto/pedidos
        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: COP {p['price']} (stock {p['stock']})"
            variantes = p.get('product_variants') or []
            if variantes:
                opts = ", ".join(
                    f"{','.join(f'{k}:{v}' for k, v in v['options'].items())} (stock {v['stock']})" for v in variantes
                )
                line += f" | Variantes: {opts}"
            imgs = p.get('product_images') or []
            if imgs:
                line += f" | Imágenes: {len(imgs)}"
            contexto_lines.append(line)
        contexto = "Catálogo actual:\n" + "\n".join(contexto_lines)
        print("🔍 [DEBUG] Contexto para LLM:\n", contexto)

        instrucciones = (
            f"{raw_text}\n\n{contexto}\n\n"
            "INSTRUCCIONES:\n"
            "1. Si no hay stock, sugiere alternativa.\n"
            "2. Si quiere comprar, detalla precio+envío y pregunta si desea algo más.\n"
            "3. Recomienda un producto adicional.\n"
            "4. Si no, pide datos y devuelve JSON exacto."
        )
        user_histories[from_number].append({"role": "user", "text": instrucciones})
        llm_resp2 = await ask_gemini_with_history(user_histories[from_number])
        print("💬 [DEBUG] LLM order flow response:\n", llm_resp2)

        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(llm_resp2)
        print("🔍 [DEBUG] order_data:", order_data)

        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)

        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
                send_whatsapp_message(from_number, f"🧠 Podrías acompañarlo con:\n{texto_rec}\n¿Te interesa alguno?")

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
