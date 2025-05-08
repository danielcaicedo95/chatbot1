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
from app.utils.extractors import extract_order_data

REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages")

        if not messages:
            return  # No es un mensaje del usuario. Ignorar.

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")

        if not raw_text or not from_number:
            return

        # Guardar mensaje en historial y en Supabase
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        productos = await get_all_products()
        if not productos:
            await send_whatsapp_message(from_number, "Lo siento, no hay productos disponibles en este momento.")
            return

        def extract_labels(obj):
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

        choice_map = {}
        for producto in productos:
            nombre = producto.get("name", "").strip().lower()
            if not nombre:
                continue
            choice_map[nombre] = (producto, None)
            for variante in producto.get("product_variants", []):
                etiquetas = extract_labels(variante.get("options", {}))
                for etiqueta in etiquetas:
                    etiqueta_normalizada = etiqueta.strip().lower()
                    if etiqueta_normalizada:
                        choice_map[etiqueta_normalizada] = (producto, variante)

        def build_catalog(productos):
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
                catalog.append({"name": p["name"], "variants": variants, "images": main_imgs})
            return catalog

        def match_target_in_catalog(catalog, productos, target):
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

        async def handle_image_request():
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

            try:
                action = json.loads(re.search(r"\{[\s\S]*\}", llm_resp).group())
            except Exception:
                return False

            if not action.get("want_images"):
                return False

            prod, var = match_target_in_catalog(catalog, productos, action.get("target", ""))
            if not prod:
                await send_whatsapp_message(from_number, "Lo siento, no encontré imágenes para eso. ¿Algo más?")
                return True

            urls = var["images"] if var else [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") is None]
            if not urls:
                await send_whatsapp_message(from_number, f"No encontré imágenes para {prod['name']}.")
                return True

            display = var["label"] if var else prod["name"]
            await send_whatsapp_message(from_number, f"¡Claro! 😊 Aquí las imágenes de *{display}*:")
            for u in urls:
                try:
                    await send_whatsapp_image(from_number, u, caption=display)
                except Exception as e:
                    print(f"❌ Error enviando imagen {u}: {e}")
                    await send_whatsapp_message(from_number, f"No pude enviar una imagen de {display}.")
            return True

        handled = await handle_image_request()
        if handled:
            return

        def build_order_context(productos):
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
                    print(f"⚠️ Error en línea catálogo: {p.get('name')} -> {e}")
            return "Catálogo actual:\n" + "\n".join(contexto_lines)

        instrucciones = (
            f"{raw_text}\n\n{build_order_context(productos)}\n\n"
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

        hist2 = [m for m in user_histories[from_number] if m["role"] in ("user", "model")]
        llm_resp2 = await ask_gemini_with_history(hist2 + [{"role": "user", "text": instrucciones}])
        order_data, clean_text = extract_order_data(llm_resp2)

        user_histories[from_number].append({
            "role": "model",
            "text": clean_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", clean_text)

        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
                await send_whatsapp_message(
                    from_number,
                    f"🧠 Podrías acompañar tu pedido con:\n{texto_rec}\n¿Te interesa alguno?"
                )

        if not order_data:
            await send_whatsapp_message(from_number, clean_text)
        else:
            result = await process_order(from_number, order_data)
            status = result.get("status")
            if status == "missing":
                campos = "\n".join(f"- {f.replace('_',' ')}" for f in result.get("fields", []))
                await send_whatsapp_message(from_number, f"📋 Faltan datos:\n{campos}")
            elif status == "created":
                await send_whatsapp_message(from_number, "✅ Pedido confirmado. ¡Gracias! 🎉")
            elif status == "updated":
                await send_whatsapp_message(from_number, "♻️ Pedido actualizado correctamente.")
            else:
                await send_whatsapp_message(from_number, "❌ Error guardando el pedido.")

    except Exception:
        print("❌ [ERROR en handle_user_message]:\n", traceback.format_exc())
        if 'from_number' in locals():
            try:
                await send_whatsapp_message(from_number, "❌ Ocurrió un error. Intenta de nuevo.")
            except Exception:
                print("⚠️ No se pudo enviar mensaje de error al usuario.")
