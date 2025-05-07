
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
        text = raw_text.lower()
        from_number = msg.get("from")
        print(f"🔍 [DEBUG] From: {from_number}, Text: '{raw_text}'")
        if not raw_text or not from_number:
            print("⚠️ [DEBUG] Missing text or from_number")
            return

        # 2) Guardar en historial local y Supabase
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)
        print("🔍 [DEBUG] User message saved")

        # 3) Saludo inicial si es primera interacción
        if len(user_histories[from_number]) == 1:
            saludo = "¡Hola! 👋 Soy Lucas, tu asistente de Licores El Roble. ¿En qué puedo ayudarte hoy?"
            user_histories[from_number].append({"role": "model", "text": saludo, "time": datetime.utcnow().isoformat()})
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # 4) Obtener catálogo completo con variantes e imágenes
        productos = await get_all_products()
        nombres = [p["name"] for p in productos]

        # 5) Detectar si es petición de imágenes
        if re.search(r"\bfoto(s)?\b|\bimagen(es)?\b", text):
            # Uso de IA para extraer intención y producto exacto
            prompt = (
                "Eres un asistente que detecta si el usuario quiere ver imágenes de un producto.\n"
                f"Catálogo: {', '.join(nombres)}.\n"
                f"Mensaje: '{raw_text}'.\n"
                "Responde con un JSON válido sin Markdown, ejemplo:"
                "{'send_images': true, 'product_name': 'Vodka Absolut'} o {'send_images': false}."
            )
            llm_input = user_histories[from_number][-10:] + [{"role": "user", "text": prompt}]
            llm_resp = await ask_gemini_with_history(llm_input)
            print("🔍 [DEBUG] Raw LLM image intent response:\n", llm_resp)

            # Extraer JSON de respuesta
            json_match = re.search(r"\{[\s\S]*\}", llm_resp)
            action = {"send_images": False}
            if json_match:
                try:
                    action = json.loads(json_match.group())
                except Exception as e:
                    print(f"⚠️ [DEBUG] JSON parse error: {e}")
            print("🔍 [DEBUG] Parsed action:", action)

            # Si la IA confirma envío de imágenes
            if action.get("send_images"):
                prod_name = action.get("product_name", "").strip()
                send_all = prod_name.lower().startswith("todos")

                # Coincidencia exacta o difusa
                if not send_all and prod_name not in nombres:
                    matches = get_close_matches(prod_name, nombres, n=1, cutoff=0.6)
                    if matches:
                        prod_name = matches[0]
                        print(f"🔍 [DEBUG] Fuzzy matched to '{prod_name}'")
                    else:
                        send_whatsapp_message(from_number, f"No encontré '{prod_name}'. ¿Puedes verificar el nombre? 😕")
                        return

                # Confirmación humana
                if send_all:
                    send_whatsapp_message(from_number, "¡Claro! 😊 Te envío imágenes de todos nuestros productos...")
                else:
                    send_whatsapp_message(from_number, f"¡Claro! 😊 Buscando imágenes de *{prod_name}*...")

                # Recopilar imágenes (producto vs variante)
                urls = []
                # Determinar targets: producto o todos
                if send_all:
                    targets = productos
                else:
                    base_product = next((p for p in productos if p["name"] == prod_name), None)
                    if not base_product:
                        send_whatsapp_message(from_number, f"No encontré el producto {prod_name}.")
                        return
                    # Detectar variante
                    variant = None
                    for v in base_product.get("product_variants", []):
                        opts = [str(val).lower() for val in v.get("options", {}).values()]
                        if any(opt in prod_name.lower() for opt in opts):
                            variant = v
                            break
                    if variant:
                        print(f"🔍 [DEBUG] Detected variant: {variant.get('options')}")
                        targets = []
                        for img in variant.get("product_images", []):
                            url = img.get("url")
                            if url:
                                urls.append((prod_name, url))
                    else:
                        targets = [base_product]

                # Recopilar de targets: productos y variantes si corresponde
                for producto in targets:
                    for img in producto.get("product_images", []):
                        url = img.get("url")
                        if url:
                            urls.append((producto["name"], url))
                    if send_all or variant is None:
                        for v in producto.get("product_variants", []):
                            for img in v.get("product_images", []):
                                url = img.get("url")
                                if url:
                                    urls.append((producto["name"], url))

                # Eliminar duplicados preservando orden
                seen = set()
                unique = []
                for name, url in urls:
                    if url not in seen:
                        seen.add(url)
                        unique.append((name, url))

                # Filtrar formatos compatibles
                supported = []
                for name, url in unique:
                    if url.lower().endswith((".png", ".jpg", ".jpeg")):
                        supported.append((name, url))
                    else:
                        print(f"⚠️ [DEBUG] URL skip unsupported format: {url}")

                if not supported:
                    send_whatsapp_message(from_number, "Lo siento, las imágenes disponibles no están en un formato compatible con WhatsApp. 😔")
                    return

                # Envío robusto
                for name, url in supported:
                    try:
                        send_whatsapp_image(from_number, url, caption=name)
                    except Exception as e:
                        print(f"❌ [ERROR] sending image {url}: {e}")
                        send_whatsapp_message(from_number, f"Ocurrió un error enviando la imagen de {name}.")
                return

        # 6) Construir contexto rico (texto)
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

        # 7) Instrucciones para el modelo de pedidos
        instrucciones = (
            f"{raw_text}\n\n{contexto}\n\n"
            "INSTRUCCIONES:\n"
            "1. Si un producto no está disponible, sugiere alternativa.\n"
            "2. Si hay intención de compra, detalla productos, cantidad y precio, más COP 5.000 de envío.\n"
            "3. Recomienda un producto adicional.\n"
            "4. Si el usuario dice 'no', solicita nombre, dirección, teléfono y método de pago.\n"
            "5. Al final, incluye un JSON exacto en 'order_details'."
        )
        user_histories[from_number].append({"role": "user", "text": instrucciones})
        llm_resp2 = await ask_gemini_with_history(user_histories[from_number])
        print("💬 [DEBUG] LLM order flow response:\n", llm_resp2)

        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(llm_resp2)
        print("🔍 [DEBUG] order_data:\n", order_data)
        print("🔍 [DEBUG] clean_text:\n", clean_text)

        # 8) Guardar respuesta limpia
        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)

        # 9) Recomendaciones y mensajes finales
        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
                send_whatsapp_message(
                    from_number,
                    f"🧠 Podrías acompañar tu pedido con:\n{texto_rec}\n¿Te interesa alguno?"
                )

        if not order_data:
            send_whatsapp_message(from_number, clean_text)
        else:
            result = await process_order(from_number, order_data)
            status = result.get("status")
            if status == "missing":
                campos = "\n".join(f"- {f.replace('_', ' ')}" for f in result.get("fields", []))
                send_whatsapp_message(from_number, f"📋 Faltan datos:\n{campos}")
            elif status == "created":
                send_whatsapp_message(from_number, "✅ Pedido confirmado. ¡Gracias! 🎉")
            elif status == "updated":
                send_whatsapp_message(from_number, "♻️ Pedido actualizado correctamente.")
            else:
                send_whatsapp_message(from_number, "❌ Error guardando el pedido.")

    except Exception:
        print("❌ [ERROR] in handle_user_message:\n", traceback.format_exc())