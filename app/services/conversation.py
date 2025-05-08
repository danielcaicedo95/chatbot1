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
        change = entry.get("changes", [{}])[0]
        messages = change.get("value", {}).get("messages") or []
        if not messages:
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        if not raw_text or not from_number:
            return

        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        productos = await get_all_products()
        if not productos:
            await send_whatsapp_message(from_number, "Lo siento, no tenemos productos disponibles ahora.")
            return

        def extract_labels(o):
            labels = []
            if isinstance(o, dict):
                for v in o.values(): labels += extract_labels(v)
            elif isinstance(o, list):
                for v in o: labels += extract_labels(v)
            elif isinstance(o, str):
                labels.append(o)
            return labels

        def build_catalog(prod_list):
            catalog = []
            for p in prod_list:
                variants = []
                for v in p.get("product_variants", []):
                    opts = v.get("options", {})
                    if not opts:
                        continue
                    key, val = next(iter(opts.items()))
                    value = str(val).lower()
                    label = v.get("variant_label") or f"{key}:{value}"
                    imgs = [img["url"] for img in p.get("product_images", []) if img.get("variant_id") == v["id"]]
                    variants.append({
                        "id": v["id"],
                        "value": value,
                        "label": label,
                        "images": imgs,
                        "price": v.get("price"),
                        "stock": v.get("stock")
                    })
                main_imgs = [img["url"] for img in p.get("product_images", []) if img.get("variant_id") is None]
                catalog.append({
                    "name": p["name"],
                    "price": p.get("price"),
                    "stock": p.get("stock"),
                    "variants": variants,
                    "images": main_imgs
                })
            return catalog

        def match_target(catalog, target):
            t = target.strip().lower()
            for e in catalog:
                for v in e["variants"]:
                    if v["value"] == t:
                        return e, v
            for e in catalog:
                if e["name"].lower() == t:
                    return e, None
            for e in catalog:
                for v in e["variants"]:
                    if v["value"] in t:
                        return e, v
            choices = [v["value"] for e in catalog for v in e["variants"]] + [e["name"].lower() for e in catalog]
            best = get_close_matches(t, choices, n=1, cutoff=0.5)
            if best:
                b = best[0]
                for e in catalog:
                    for v in e["variants"]:
                        if v["value"] == b:
                            return e, v
                    if e["name"].lower() == b:
                        return e, None
            return None, None

        catalog = build_catalog(productos)
        prompt = {
            "user_request": raw_text,
            "catalog": catalog,
            "instructions": [
                "Devuelve JSON EXACTO sin Markdown:",
                "  {'want_images': true, 'target': 'valor variante o nombre producto'}",
                "o si no pide imágenes:",
                "  {'want_images': false}"
            ]
        }

        hist = user_histories[from_number][-10:]
        hist.append({"role": "user", "text": json.dumps(prompt, ensure_ascii=False)})
        llm_mult = await ask_gemini_with_history(hist)

        try:
            action = json.loads(re.search(r"\{[\s\S]*\}", llm_mult).group())
        except Exception as e:
            print("⚠️ No se pudo interpretar JSON del modelo:", e)
            action = {"want_images": False}

        if action.get("want_images"):
            prod_sel, var_sel = match_target(catalog, action.get("target", ""))
            if not prod_sel:
                await send_whatsapp_message(from_number, "No encontré ese producto para mostrar imágenes.")
                return

            imgs = var_sel["images"] if var_sel else prod_sel["images"]
            if not imgs:
                await send_whatsapp_message(from_number, f"No hay imágenes disponibles de {prod_sel['name']}.")
                return

            title = var_sel["label"] if var_sel else prod_sel["name"]
            await send_whatsapp_message(from_number, f"Imágenes de *{title}*:")
            for url in imgs:
                await send_whatsapp_image(from_number, url, caption=title)
            return

        def build_order_context(cat):
            lines = []
            for e in cat:
                line = f"- {e['name']}: COP {e['price']} (stock {e['stock']})"
                if e["variants"]:
                    vlines = [f"{v['label']} (COP {v['price']}, stock {v['stock']})" for v in e["variants"]]
                    line += " | Variantes: " + "; ".join(vlines)
                if e["images"]:
                    line += f" | Imágenes: {len(e['images'])}"
                lines.append(line)
            return "Catálogo actual:\n" + "\n".join(lines)

        instrucciones = (
            f"{raw_text}\n\n{build_order_context(catalog)}\n\n"
            "INSTRUCCIONES:\n"
            "1. Si un producto no está disponible, sugiere alternativa.\n"
            "2. Si hay intención de compra, incluye:\n"
            "   - Productos, cantidad y precio\n"
            "   - Subtotal + COP 5.000 envío\n"
            "   - ¿Deseas algo más?\n"
            "   - Recomienda 1 producto adicional\n"
            "   - Si 'no', pide nombre, dirección, teléfono y pago.\n"
            "3. Usa emojis y tono cercano.\n"
            "4. Al confirmar, al final incluye este JSON EXACTO:\n"
            "{\"order_details\":{\"name\":\"NOMBRE\",\"address\":\"DIRECCIÓN\",\"phone\":\"TELÉFONO\","
            "\"payment_method\":\"TIPO_PAGO\",\"products\":[{\"name\":\"NOMBRE\",\"quantity\":1,"
            "\"price\":0}],\"total\":0}}"
        )

        hist2 = user_histories[from_number][-10:]
        hist2.append({"role": "user", "text": instrucciones})
        llm_order = await ask_gemini_with_history(hist2)

        order_data, model_text = extract_order_data(llm_order)

        user_histories[from_number].append({
            "role": "model",
            "text": model_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", model_text)

        if order_data.get("products"):
            recs = await get_recommended_products(order_data["products"])
            if recs:
                text_recs = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recs)
                await send_whatsapp_message(from_number, f"🧠 Tal vez te interese:\n{text_recs}\n¿Te animas?")

        if not order_data.get("products"):
            await send_whatsapp_message(from_number, model_text)
        else:
            result = await process_order(from_number, order_data)
            status = result.get("status")
            if status == "missing":
                campos = "\n".join(f"- {f.replace('_', ' ')}" for f in result.get("fields", []))
                await send_whatsapp_message(from_number, f"📋 Faltan datos:\n{campos}")
            elif status == "created":
                await send_whatsapp_message(from_number, "✅ Pedido recibido. ¡Gracias! 🎉")
            elif status == "updated":
                await send_whatsapp_message(from_number, "♻️ Pedido actualizado.")
            else:
                await send_whatsapp_message(from_number, "❌ Error guardando tu pedido.")

    except Exception:
        print("❌ [ERROR] en handle_user_message:\n", traceback.format_exc())
        if 'from_number' in locals():
            await send_whatsapp_message(from_number, "❌ Ocurrió un error inesperado. Intenta más tarde.")
