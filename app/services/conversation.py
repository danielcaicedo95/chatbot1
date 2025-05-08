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
        # ─── 1) Depurar payload y extraer mensaje ───────────────────────────────
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

        # ─── 2) Guardar en historial y Supabase ───────────────────────────────────
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # ─── 3) Saludo inicial ───────────────────────────────────────────────────
        # Contamos solo mensajes user/model para evitar contar context
        count_conv = len([m for m in user_histories[from_number] if m["role"] in ("user","model")])
        if count_conv == 1:
            saludo = "¡Hola! 👋 Soy Lucas, tu asistente de Licores El Roble. ¿En qué puedo ayudarte hoy?"
            user_histories[from_number].append({
                "role": "model",
                "text": saludo,
                "time": datetime.utcnow().isoformat()
            })
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # ─── 4) Cargar catálogo y preparar choice_map ─────────────────────────────
        productos = await get_all_products()
        # Mapa lowercase -> (producto_obj, variante_obj or None)
        choice_map = {}
        for p in productos:
            choice_map[p["name"].lower()] = (p, None)
            for v in p.get("product_variants", []):
                for val in v.get("options", {}).values():
                    choice_map[str(val).lower()] = (p, v)

        # ─── 5) BLOQUE MULTIMEDIA SIN PALABRAS CLAVE ───────────────────────────────
        # 5.1) Armar prompt para Gemini
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
        # 5.2) Filtrar historial (solo user/model) para Gemini
        hist = [m for m in user_histories[from_number] if m["role"] in ("user","model")]
        llm_input = hist[-10:] + [{"role": "user", "text": json.dumps(prompt_obj, ensure_ascii=False)}]
        llm_resp = await ask_gemini_with_history(llm_input)
        print("🔍 [DEBUG] Raw LLM multimedia response:\n", llm_resp)

        # 5.3) Parsear JSON de Gemini
        action = {"want_images": False}
        json_match = re.search(r"\{[\s\S]*\}", llm_resp)
        if json_match:
            try:
                action = json.loads(json_match.group())
            except Exception as e:
                print("⚠️ [DEBUG] JSON parse error:", e)
        print("🔍 [DEBUG] Parsed multimedia action:", action)

        # 5.4) Si Gemini indica want_images, procesar
        if action.get("want_images"):
            target = action.get("target", "").strip().lower()

            # Si no vino target, hacemos fallback a la última selección guardada
            if not target:
                for e in reversed(user_histories[from_number]):
                    if e.get("role") == "context":
                        target = e["last_image_selection"]["product_name"].lower()
                        break

            # Match insensible a mayúsculas
            match = get_close_matches(target, list(choice_map.keys()), n=1, cutoff=0.4)
            if match:
                prod, var = choice_map[match[0]]

                # Guardar nueva selección en memoria (no se envía a Gemini)
                user_histories[from_number].append({
                    "role": "context",
                    "text": f"ctx:{match[0]}",  # campo text para consistencia
                    "last_image_selection": {
                        "product_name": prod["name"],
                        "variant_id": var["id"] if var else None
                    },
                    "time": datetime.utcnow().isoformat()
                })

                # 5.5) Recopilar URLs: variante si existe, sino imagen principal
                if var and var.get("product_images"):
                    urls = [
                        img["url"]
                        for img in var["product_images"]
                        if img["url"].lower().endswith((".png", ".jpg", ".jpeg"))
                    ]
                else:
                    urls = []
                    imgs = prod.get("product_images", [])
                    if imgs:
                        u = imgs[0]["url"]
                        if u.lower().endswith((".png", ".jpg", ".jpeg")):
                            urls = [u]

                # 5.6) Enviar imágenes
                if urls:
                    send_whatsapp_message(from_number, f"¡Claro! 😊 Aquí la(s) imagen(es) de *{prod['name']}*:")
                    for url in urls:
                        try:
                            send_whatsapp_image(from_number, url, caption=prod["name"])
                        except Exception as e:
                            print(f"❌ [ERROR] sending image {url}: {e}")
                            send_whatsapp_message(from_number, f"No pude enviar la imagen de {prod['name']}.")
                    return

            # Si no hubo match o no hay URLs
            send_whatsapp_message(from_number, "Lo siento, no encontré imágenes para eso. ¿Algo más?")
            return

        # ─── 6) FIN BLOQUE MULTIMEDIA ──────────────────────────────────────────────

        # ─── 7) Construir contexto textual para flujo de pedidos ─────────────────
        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: COP {p['price']} (stock {p['stock']})"
            variantes = p.get("product_variants") or []
            if variantes:
                opts = []
                for v in variantes:
                    opts.append(
                        ",".join(f"{k}:{v2}" for k, v2 in v["options"].items())
                        + f" (stock {v['stock']})"
                    )
                line += " | Variantes: " + "; ".join(opts)
            imgs = p.get("product_images") or []
            if imgs:
                line += f" | Imágenes: {len(imgs)}"
            contexto_lines.append(line)
        contexto = "Catálogo actual:\n" + "\n".join(contexto_lines)
        print("🔍 [DEBUG] Contexto construido:\n", contexto)

        # ─── 8) Instrucciones y llamada a Gemini para el flujo de pedidos ────────
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
        hist2 = [m for m in user_histories[from_number] if m["role"] in ("user","model")]
        llm_resp2 = await ask_gemini_with_history(hist2 + [{"role": "user", "text": instrucciones}])
        print("💬 [DEBUG] LLM order flow response:\n", llm_resp2)

        # ─── 9) Extraer y procesar pedido ────────────────────────────────────────
        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(llm_resp2)
        print("🔍 [DEBUG] order_data:\n", order_data)
        print("🔍 [DEBUG] clean_text:\n", clean_text)

        user_histories[from_number].append({
            "role": "model",
            "text": clean_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", clean_text)

        # ─── 10) Recomendaciones y procesamiento de orden ────────────────────────
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
