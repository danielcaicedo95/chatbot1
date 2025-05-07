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

        # === BLOQUE MULTIMEDIA SIN PALABRAS CLAVE ===
        # Preguntamos al LLM si el usuario quiere imágenes y de qué
        prompt_obj = {
            "user_request": raw_text,
            "catalog": [
                {
                    "name": p["name"],
                    "variants": [v["options"] for v in p.get("product_variants", [])]
                } for p in productos
            ],
            "instructions": [
                "Devuelve JSON EXACTO sin Markdown:",
                "  {'want_images': true, 'target': 'nombre producto o variante'}",
                "o si no pide imágenes:",
                "  {'want_images': false}"
            ]
        }
        llm_input = user_histories[from_number][-10:] + [{"role": "user", "text": json.dumps(prompt_obj, ensure_ascii=False)}]
        llm_resp = await ask_gemini_with_history(llm_input)
        print("🔍 [DEBUG] Raw LLM multimedia response:\n", llm_resp)

        # Extraer acción JSON
        action = {"want_images": False}
        json_match = re.search(r"\{[\s\S]*\}", llm_resp)
        if json_match:
            try:
                action = json.loads(json_match.group())
            except Exception as e:
                print("⚠️ [DEBUG] JSON parse error:", e)
        print("🔍 [DEBUG] Parsed multimedia action:", action)

        if action.get("want_images"):
            target = action.get("target", "").strip()
            # Construir lista de posibles matches (nombres y opciones)
            choices = nombres + [
                str(opt) for p in productos
                for v in p.get("product_variants", [])
                for opt in v.get("options", {}).values()
            ]
            match = get_close_matches(target, choices, n=1, cutoff=0.5)
            if match:
                sel = match[0]
                # Encontrar producto y variante
                producto = next((p for p in productos if p["name"] == sel), None)
                variante = None
                if not producto:
                    for p in productos:
                        for v in p.get("product_variants", []):
                            if sel in v.get("options", {}).values():
                                producto, variante = p, v
                                break
                        if producto:
                            break

                # Recopilar URLs compatibles
                urls = []
                if producto:
                    urls += [img["url"] for img in producto.get("product_images", [])]
                if variante:
                    urls += [img["url"] for img in variante.get("product_images", [])]
                urls = [u for u in urls if u.lower().endswith((".png", ".jpg", ".jpeg"))]

                if urls:
                    send_whatsapp_message(from_number, f"¡Claro! 😊 Aquí tienes las imágenes de *{sel}*:")
                    for url in urls:
                        try:
                            send_whatsapp_image(from_number, url, caption=sel)
                        except Exception as e:
                            print(f"❌ [ERROR] sending image {url}: {e}")
                            send_whatsapp_message(from_number, f"No pude enviar la imagen de {sel}.")
                    return
            # Si no hay match o no hay imágenes
            send_whatsapp_message(from_number, "Lo siento, no encontré imágenes para eso. ¿Algo más en lo que te pueda ayudar?")
            return

        # === FIN BLOQUE MULTIMEDIA ===

        # 5) Construir contexto rico (texto) tal como antes
        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: COP {p['price']} (stock {p['stock']})"
            variantes = p.get('product_variants') or []
            if variantes:
                opts = []
                for v in variantes:
                    opts.append(
                        f"{','.join(f'{k}:{v2}' for k, v2 in v['options'].items())} (stock {v['stock']})"
                    )
                line += f" | Variantes: {', '.join(opts)}"
            imgs = p.get('product_images') or []
            if imgs:
                line += f" | Imágenes: {len(imgs)}"
            contexto_lines.append(line)
        contexto = "Catálogo actual:\n" + "\n".join(contexto_lines)
        print("🔍 [DEBUG] Contexto construido:\n", contexto)

        # 6) Instrucciones para el modelo de pedidos (incluye payment_method)
        instrucciones = (
            f"{raw_text}\n\n{contexto}\n\n"
            "INSTRUCCIONES:\n"
            "1. Si un producto no está disponible, sugiere alternativa.\n"
            "2. Si hay intención de compra, detalla:\n"
            "   - Productos, cantidad y precio\n"
            "   - Subtotal + COP 5.000 envío\n"
            "   - ¿Deseas algo más?\n"
            "   - Recomienda 1 producto adicional\n"
            "   - Si 'no', pide nombre, dirección, teléfono y pago.\n"
            "3. Usa emojis y tono cercano.\n"
            "4. Al confirmar, al final incluye este JSON EXACTO:\n"
            "{\"order_details\":{\"name\":\"NOMBRE\",\"address\":\"DIRECCIÓN\",\"phone\":\"TELÉFONO\",\"payment_method\":\"TIPO_PAGO\",\"products\":[{\"name\":\"NOMBRE\",\"quantity\":1,\"price\":0}],\"total\":0}}"
        )
        user_histories[from_number].append({"role": "user", "text": instrucciones})
        llm_resp2 = await ask_gemini_with_history(user_histories[from_number])
        print("💬 [DEBUG] LLM order flow response:\n", llm_resp2)

        # 7) Extraer y procesar pedido como antes
        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(llm_resp2)
        print("🔍 [DEBUG] order_data:\n", order_data)
        print("🔍 [DEBUG] clean_text:\n", clean_text)

        # Guardar y enviar respuesta limpia
        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)

        # 8) Recomendaciones si aplica
        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
                send_whatsapp_message(from_number,
                    f"🧠 Podrías acompañar tu pedido con:\n{texto_rec}\n¿Te interesa alguno?"
                )

        # 9) Procesar orden
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
