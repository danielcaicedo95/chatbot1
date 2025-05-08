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
            user_histories[from_number].append({
                "role": "model",
                "text": saludo,
                "time": datetime.utcnow().isoformat()
            })
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # 4) Obtener catálogo completo con variantes e imágenes
        productos = await get_all_products()
        nombres = [p["name"] for p in productos]

        # === BLOQUE MULTIMEDIA SIN PALABRAS CLAVE ===
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
        llm_input = user_histories[from_number][-10:] + [
            {"role": "user", "text": json.dumps(prompt_obj, ensure_ascii=False)}
        ]
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
            # 1) Tomar target explícito o fallback a última selección
            target = action.get("target", "").strip()
            if not target:
                producto_name = None
                variant_id = None
                for entry in reversed(user_histories[from_number]):
                    if entry.get("role") == "context":
                        ctx = entry.get("last_image_selection", {})
                        producto_name = ctx.get("product_name")
                        variant_id = ctx.get("variant_id")
                        break
                target = producto_name or ""

            # 2) Match
            choices = nombres + [
                str(opt) for p in productos
                for v in p.get("product_variants", [])
                for opt in v.get("options", {}).values()
            ]
            match = get_close_matches(target, choices, n=1, cutoff=0.5)
            if match:
                sel = match[0]
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

                # ─── GUARDAR EN MEMORIA ESTA SELECCIÓN ───
                user_histories[from_number].append({
                    "role": "context",
                    # Añadimos 'text' para no romper la expectativa de Gemini
                    "text": f"last_selection:{sel}",
                    "last_image_selection": {
                        "product_name": producto["name"] if producto else None,
                        "variant_id": variante.get("id") if variante else None
                    },
                    "time": datetime.utcnow().isoformat()
                })

                # 3) Selección de URLs (solo variante o solo principal)
                if variante and variante.get("product_images"):
                    urls = [
                        img["url"] for img in variante["product_images"]
                        if img["url"].lower().endswith((".png", ".jpg", ".jpeg"))
                    ]
                elif producto and producto.get("product_images"):
                    first = producto["product_images"][0]
                    urls = [first["url"]] if first["url"].lower().endswith((".png", ".jpg", ".jpeg")) else []
                else:
                    urls = []

                # 4) Envío
                if urls:
                    send_whatsapp_message(from_number, f"¡Claro! 😊 Aquí tienes la(s) imagen(es) de *{sel}*:")
                    for url in urls:
                        try:
                            send_whatsapp_image(from_number, url, caption=sel)
                        except Exception as e:
                            print(f"❌ [ERROR] sending image {url}: {e}")
                            send_whatsapp_message(from_number, f"No pude enviar la imagen de {sel}.")
                    return

            send_whatsapp_message(
                from_number,
                "Lo siento, no encontré imágenes para eso. ¿Algo más en lo que te pueda ayudar?"
            )
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

        # 6) Instrucciones para el modelo de pedidos (igual que antes)...
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

        # 7) Procesar pedido (igual que antes)...
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

        # 8) Recomendaciones y 9) Procesar orden (igual que antes)...

    except Exception:
        print("❌ [ERROR] in handle_user_message:\n", traceback.format_exc())
