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

# Función principal
async def handle_user_message(body: dict):
    try:
        # ─── 1) Validar y extraer mensaje del webhook ─────────────────────────────
        print("🔍 [DEBUG] Incoming webhook payload:")
        print(json.dumps(body, indent=2, ensure_ascii=False))

        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages")

        if not messages:
            print("⚠️ [DEBUG] No se encontraron mensajes en el payload.")
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")

        if not raw_text or not from_number:
            print("⚠️ [DEBUG] Faltan campos obligatorios: texto o número de origen.")
            return

        print(f"🔍 [DEBUG] Mensaje recibido de {from_number}: '{raw_text}'")

        # Aquí seguiría tu lógica completa...
    
    except Exception:
        print("❌ [ERROR] en handle_user_message:\n", traceback.format_exc())


        # ─── 2) Registrar mensaje en historial y Supabase ────────────────────────
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })

        await save_message_to_supabase(from_number, "user", raw_text)


        # ─── 3) Cargar catálogo y construir mapa de opciones ────────────────────────────
        try:
            productos = await get_all_products()
            if not productos:
                print("⚠️ [DEBUG] No se encontraron productos en el catálogo.")
                await send_whatsapp_message(from_number, "Lo siento, no hay productos disponibles en este momento.")
                return

            # choice_map: texto usuario → (producto, variante) posible
            choice_map = {}

            def extract_labels(obj) -> list[str]:
                """Extrae todas las etiquetas tipo string de un objeto anidado."""
                labels = []

                def _extract(o):
                    if isinstance(o, dict):
                        for v in o.values():
                            _extract(v)
                    elif isinstance(o, list):
                        for v in o:
                            _extract(v)
                    elif isinstance(o, str):
                        labels.append(o)

                _extract(obj)
                return labels

            for producto in productos:
                nombre = producto.get("name", "").strip().lower()
                if not nombre:
                    continue

                choice_map[nombre] = (producto, None)

                for variante in producto.get("product_variants", []):
                    opciones = variante.get("options", {})
                    etiquetas = extract_labels(opciones)
                    for etiqueta in etiquetas:
                        etiqueta_normalizada = etiqueta.strip().lower()
                        if etiqueta_normalizada:
                            choice_map[etiqueta_normalizada] = (producto, variante)

            print(f"🔍 [DEBUG] choice_map generado con {len(choice_map)} entradas.")

        except Exception as e:
            print("❌ [ERROR] al cargar el catálogo o construir el choice_map:")
            print(traceback.format_exc())
            await send_whatsapp_message(from_number, "Ocurrió un error al procesar el catálogo. Intenta más tarde.")
            return

       # ─── 5) BLOQUE MULTIMEDIA SIN PALABRAS CLAVE ───────────────────────────────

        def build_catalog(productos: list[dict]) -> list[dict]:
            catalog = []
            for p in productos:
                variants = []
                for v in p.get("product_variants", []):
                    opts = v.get("options", {})
                    if not opts:
                        continue
                    value = next(iter(opts.values())).lower()
                    key0 = next(iter(opts.keys()))
                    label = v.get("variant_label") or f"{key0}:{value}"
                    imgs = [img["url"] for img in p.get("product_images", []) if img.get("variant_id") == v["id"]]
                    variants.append({"id": v["id"], "value": value, "label": label, "images": imgs})

                main_imgs = [img["url"] for img in p.get("product_images", []) if img.get("variant_id") is None]

                catalog.append({
                    "name": p["name"],
                    "variants": variants,
                    "images": main_imgs
                })
            return catalog

        def match_target_in_catalog(catalog, productos, target):
            from difflib import get_close_matches
            target = target.strip().lower()

            for entry in catalog:
                for v in entry["variants"]:
                    if v["value"] == target:
                        prod = next(p for p in productos if p["name"] == entry["name"])
                        return prod, v

            for entry in catalog:
                if entry["name"].lower() == target:
                    prod = next(p for p in productos if p["name"] == entry["name"])
                    return prod, None

            for entry in catalog:
                for v in entry["variants"]:
                    if v["value"] in target:
                        prod = next(p for p in productos if p["name"] == entry["name"])
                        return prod, v

            choices = [v["value"] for e in catalog for v in e["variants"]] + [e["name"].lower() for e in catalog]
            match = get_close_matches(target, choices, n=1, cutoff=0.5)
            if match:
                m0 = match[0]
                for entry in catalog:
                    for v in entry["variants"]:
                        if v["value"] == m0:
                            prod = next(p for p in productos if p["name"] == entry["name"])
                            return prod, v
                for entry in catalog:
                    if entry["name"].lower() == m0:
                        prod = next(p for p in productos if p["name"] == entry["name"])
                        return prod, None

            return None, None

        async def handle_image_request(productos, raw_text, from_number, user_histories):
            catalog = build_catalog(productos)
            
            prompt_obj = {
                "user_request": raw_text,
                "catalog": catalog,
                "instructions": [
                    "Devuelve JSON EXACTO sin Markdown:",
                    "  {'want_images': true, 'target': 'valor variante o nombre producto'}",
                    "o si no pide imágenes:",
                    "  {'want_images': false}"
                ]
            }

            hist = [m for m in user_histories[from_number] if m["role"] in ("user", "model")]
            llm_input = hist[-10:] + [{"role": "user", "text": json.dumps(prompt_obj, ensure_ascii=False)}]
            llm_resp = await ask_gemini_with_history(llm_input)

            print("🔍 [DEBUG] Raw multimedia response:\n", llm_resp)

            try:
                action = json.loads(re.search(r"\{[\s\S]*\}", llm_resp).group())
            except Exception as e:
                print("⚠️ [DEBUG] JSON parse error:", e)
                action = {"want_images": False}

            print("🔍 [DEBUG] Parsed multimedia action:", action)

            if not action.get("want_images"):
                return

            prod, var = match_target_in_catalog(catalog, productos, action.get("target", ""))
            if not prod:
                send_whatsapp_message(from_number, "Lo siento, no encontré imágenes para eso. ¿Algo más?")
                return

            urls = var["images"] if var else [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") is None]
            if not urls:
                send_whatsapp_message(from_number, f"No encontré imágenes para {prod['name']}.")
                return

            display = var["label"] if var else prod["name"]
            send_whatsapp_message(from_number, f"¡Claro! 😊 Aquí las imágenes de *{display}*:")

            for u in urls:
                try:
                    send_whatsapp_image(from_number, u, caption=display)
                    print(f"✅ Enviada imagen: {u}")
                except Exception as e:
                    print(f"❌ [ERROR] sending image {u}: {e}")
                    send_whatsapp_message(from_number, f"No pude enviar una imagen de {display}.")


        # ─── 6) FIN BLOQUE MULTIMEDIA ──────────────────────────────────────────────


       # ─── 7) Construir contexto textual para flujo de pedidos ─────────────────
        def build_order_context(productos: list[dict]) -> str:
            contexto_lines = []
            for p in productos:
                try:
                    line = f"- {p['name']}: COP {p['price']} (stock {p['stock']})"
                    variantes = p.get("product_variants") or []
                    if variantes:
                        opts = []
                        for v in variantes:
                            options_str = ",".join(f"{k}:{v2}" for k, v2 in v.get("options", {}).items())
                            opts.append(f"{options_str} (stock {v.get('stock', 'N/A')})")
                        line += " | Variantes: " + "; ".join(opts)
                    if p.get("product_images"):
                        line += f" | Imágenes: {len(p['product_images'])}"
                    contexto_lines.append(line)
                except Exception as e:
                    print(f"⚠️ [DEBUG] Error construyendo línea de contexto para producto: {p.get('name')} -> {e}")
            return "Catálogo actual:\n" + "\n".join(contexto_lines)

        # ─── 8) Instrucciones y llamada a Gemini para el flujo de pedidos ────────
        order_context = build_order_context(productos)
        instrucciones = (
            f"{raw_text}\n\n{order_context}\n\n"
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

        try:
            hist2 = [m for m in user_histories[from_number] if m["role"] in ("user", "model")]
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
            print("❌ [ERROR] en flujo de pedidos:\n", traceback.format_exc())
            send_whatsapp_message(from_number, "❌ Hubo un error procesando tu pedido. Intenta de nuevo o escríbenos.")
