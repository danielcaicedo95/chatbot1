from datetime import datetime
import json
import re
import traceback

from difflib import get_close_matches
from app.utils.memory import user_histories, user_context
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import (
    send_whatsapp_message,
    send_whatsapp_image
)
try:
    from app.clients.whatsapp import send_typing_indicator
except ImportError:
    async def send_typing_indicator(_):
        pass

from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products

REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages", [])
        if not messages:
            return
        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        if not raw_text:
            return
        from_number = msg.get("from")
        if not from_number:
            return
        normalized = raw_text.lower()

        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)
        user_context.setdefault(from_number, {})

        await send_typing_indicator(from_number)

        productos = await get_all_products()
        for p in productos:
            p_price = p.get("price", 0)
            p_stock = p.get("stock", 0)
            p["price_numeric"] = p_price
            p["price"] = f"COP {p_price:,}" if p_price > 0 else "Consultar"
            p["stock"] = p_stock if p_stock > 0 else "Agotado"
            for v in p.get("product_variants", []):
                v_price = v.get("price", 0)
                v_stock = v.get("stock", 0)
                v["price"] = f"COP {v_price:,}" if v_price > 0 else "Consultar"
                v["stock"] = v_stock if v_stock > 0 else "Agotado"

        if re.search(r"\b(foto|imagen|muestra|ver)\b", normalized):
            last = user_context[from_number].get("last_selection")
            selected = None
            if last:
                selected = last
            else:
                for p in productos:
                    if p["name"].lower() in normalized:
                        selected = (p, None)
                        break
                    for v in p.get("product_variants", []):
                        if any(str(val).lower() in normalized for val in v.get("options", {}).values()):
                            selected = (p, v)
                            break
                    if selected:
                        break
            if not selected:
                await send_whatsapp_message(from_number, "Lo siento, no encontré esa imagen. ¿De qué producto hablamos? 😊")
                return
            prod, var = selected
            user_context[from_number]["last_selection"] = selected
            urls = []
            if var:
                urls = [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") == var["id"]]
            if not urls:
                urls = [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") is None]
            if not urls:
                await send_whatsapp_message(from_number, "Lo siento, no tengo imágenes disponibles de eso.")
                return
            for url in urls:
                await send_whatsapp_image(from_number, url)
            return

        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: {p['price']} (stock {p['stock']})"
            variants = p.get("product_variants", [])
            if variants:
                opts = []
                for v in variants:
                    label = ",".join(f"{k}:{v2}" for k, v2 in v["options"].items())
                    opts.append(f"{label} (stock {v['stock']}, {v['price']})")
                line += " | Variantes: " + "; ".join(opts)
            contexto_lines.append(line)
        contexto = "Catálogo:\n" + "\n".join(contexto_lines)

        await send_typing_indicator(from_number)
        instrucciones = (
            f"Usuario: {raw_text}\n{contexto}\n"
            "Actúa como un vendedor experto y amable.\n"
            "- Sé conversacional y humano, sin saludos genéricos.\n"
            "- Si no hay stock, dilo y ofrece alternativa persuasiva.\n"
            "- Si el usuario muestra intención de compra, calcula subtotal + COP 5.000 de envío y pregunta si desea algo más.\n"
            "- Sugiere un producto adicional basado en su carrito.\n"
            "- Cuando confirme, pide: nombre, dirección, teléfono y método de pago.\n"
            "- No envíes JSON al usuario."
        )
        hist = [m for m in user_histories[from_number] if m["role"] in ("user", "model")]
        llm_resp = await ask_gemini_with_history(hist + [{"role": "user", "text": instrucciones}])

        clean_text = re.sub(r"\{.*?\}", "", llm_resp, flags=re.DOTALL).strip()
        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)
        await send_typing_indicator(from_number)
        await send_whatsapp_message(from_number, clean_text)

        from app.utils.extractors import extract_order_data
        from app.services.orders import process_order
        from app.services.products import get_recommended_products

        order_data, _ = extract_order_data(llm_resp)
        if order_data and order_data.get("products"):
            lineas = []
            subtotal = 0
            for item in order_data["products"]:
                nombre = item.get("name")
                cantidad = int(item.get("quantity", 1))
                producto = next((p for p in productos if p["name"] == nombre), None)
                if not producto:
                    continue
                precio_unit = producto.get("price_numeric", 0)
                total_item = precio_unit * cantidad
                subtotal += total_item
                lineas.append(f"{cantidad} x {nombre} (COP {precio_unit:,}) = COP {total_item:,}")
                item["price"] = precio_unit

            total = subtotal + 5000
            order_data["total"] = total

            resumen = "\n".join(lineas)
            await send_whatsapp_message(
                from_number,
                f"Aquí el resumen de tu pedido:\n{resumen}\nSubtotal: COP {subtotal:,}\nEnvío: COP 5,000\nTotal: COP {total:,}\n¿Confirmas? 😊"
            )

            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(f"- {r['name']}: COP {int(r['price']):,}" for r in recomendaciones)
                await send_whatsapp_message(
                    from_number,
                    f"🧠 También te podría interesar:\n{texto_rec}\n¿Qué opinas? 😊"
                )

            result = await process_order(from_number, order_data)
            status = result.get("status")
            if status == "missing":
                faltantes = result.get("fields", [])
                campos = "\n".join(f"- {f.replace('_',' ')}" for f in faltantes)
                await send_whatsapp_message(from_number, f"📋 Falta información:\n{campos}")
            elif status == "created":
                await send_whatsapp_message(from_number, "✅ Tu pedido ha sido confirmado. ¡Gracias! 🎉")
            elif status == "updated":
                await send_whatsapp_message(from_number, "♻️ Pedido actualizado correctamente.")
            else:
                await send_whatsapp_message(from_number, "❌ Hubo un error procesando tu pedido.")
        return
    except Exception:
        traceback.print_exc()