# app/services/conversation.py

from datetime import datetime
import json
import re
import traceback

from difflib import get_close_matches
from app.utils.memory import user_histories, user_pending_data, user_context
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image

# Indicador de “escribiendo…” (stub si no existe)
try:
    from app.clients.whatsapp import send_typing_indicator
except ImportError:
    async def send_typing_indicator(_):
        pass

from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products
from app.services.orders import process_order
from app.services.products import get_recommended_products
from app.utils.extractors import extract_order_data

# Campos obligatorios para completar el formulario
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        # ─── 1) Extraer payload ─────────────────────────────────────────────────
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

        # ─── 2) Inicializar memoria y guardar histórico ─────────────────────────
        user_context.setdefault(from_number, {})
        ctx = user_context[from_number]

        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # ─── 3) Si estamos en medio del formulario, procesar campo ───────────────
        if ctx.get("awaiting_fields"):
            field = ctx["awaiting_fields"].pop(0)
            pending = user_pending_data.get(from_number, {})
            pending[field] = raw_text
            user_pending_data[from_number] = pending

            if ctx["awaiting_fields"]:
                next_field = ctx["awaiting_fields"][0]
                await send_whatsapp_message(
                    from_number,
                    f"Por favor, indícame tu {next_field.replace('_',' ')}."
                )
            else:
                # Todos los campos recibidos: guardar orden en Supabase
                result = await process_order(from_number, pending)
                status = result.get("status")
                if status in ("created", "updated"):
                    await send_whatsapp_message(
                        from_number,
                        "✅ Tu pedido ha sido procesado correctamente. ¡Gracias! 🎉"
                    )
                else:
                    await send_whatsapp_message(
                        from_number,
                        "❌ Hubo un error al procesar tu pedido. Por favor intenta de nuevo."
                    )
                ctx.pop("awaiting_fields")
            return

        # ─── 4) Simular “escribiendo…” ───────────────────────────────────────────
        await send_typing_indicator(from_number)

        # ─── 5) Cargar catálogo y formatear precios/stock ────────────────────────
        productos = await get_all_products()
        for p in productos:
            price = p.get("price", 0) or 0
            stock = p.get("stock", 0) or 0
            p["price_numeric"] = price
            p["price"] = f"COP {price:,}" if price > 0 else "Consultar"
            p["stock"] = stock if stock > 0 else "Agotado"
            for v in p.get("product_variants", []):
                v_price = v.get("price", 0) or 0
                v_stock = v.get("stock", 0) or 0
                v["price"] = f"COP {v_price:,}" if v_price > 0 else "Consultar"
                v["stock"] = v_stock if v_stock > 0 else "Agotado"

        # ─── 6) Bloque multimedia: solo si pide “foto/imagen/muestra/ver” ───────
        if re.search(r"\b(foto|imagen|muestra|ver)\b", normalized):
            # Determinar selección anterior o por nombre/variant
            selected = ctx.get("last_selection")
            if not selected:
                for p in productos:
                    if p["name"].lower() in normalized:
                        selected = (p, None)
                        break
                    for v in p.get("product_variants", []):
                        if any(str(val).lower() in normalized for val in v["options"].values()):
                            selected = (p, v)
                            break
                    if selected:
                        break

            if not selected:
                await send_whatsapp_message(
                    from_number,
                    "Lo siento, no encontré esa imagen. ¿De qué producto hablamos? 😊"
                )
                return

            prod, var = selected
            ctx["last_selection"] = selected

            # Recopilar URLs (primero variante, luego principales)
            urls = []
            if var:
                urls = [
                    img["url"] for img in prod.get("product_images", [])
                    if img.get("variant_id") == var["id"]
                ]
            if not urls:
                urls = [
                    img["url"] for img in prod.get("product_images", [])
                    if img.get("variant_id") is None
                ]
            if not urls:
                await send_whatsapp_message(
                    from_number,
                    "Lo siento, no tengo imágenes disponibles de eso."
                )
                return

            # Enviar solo la primera imagen
            await send_typing_indicator(from_number)
            await send_whatsapp_image(from_number, urls[0])
            return

        # ─── 7) Construir contexto textual para el LLM ──────────────────────────
        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: {p['price']} (stock {p['stock']})"
            variants = p.get("product_variants", [])
            if variants:
                opts = [
                    f"{','.join(f'{k}:{v2}' for k,v2 in v['options'].items())} "
                    f"(stock {v['stock']}, {v['price']})"
                    for v in variants
                ]
                line += " | Variantes: " + "; ".join(opts)
            contexto_lines.append(line)
        contexto = "Catálogo:\n" + "\n".join(contexto_lines)

        # ─── 8) Llamada a Gemini para flujo de venta ─────────────────────────────
        await send_typing_indicator(from_number)
        instrucciones = (
            f"Usuario: {raw_text}\n{contexto}\n"
            "Actúa como un vendedor experto, cercano y sin saludos genéricos.\n"
            "- Si no hay stock, dilo y sugiere alternativa persuasiva.\n"
            "- Si muestra intención de compra, calcula subtotal + COP 5.000 de envío y pregunta si desea algo más.\n"
            "- Sugiere un producto adicional basado en su carrito.\n"
            "- Cuando confirme, pide: nombre, dirección, teléfono y método de pago.\n"
            "- No envíes JSON al usuario."
        )
        hist = [m for m in user_histories[from_number] if m["role"] in ("user","model")]
        llm_resp = await ask_gemini_with_history(hist + [{"role":"user","text":instrucciones}])

        # ─── 9) Enviar respuesta humana sin JSON ───────────────────────────────
        clean_text = re.sub(r"\{.*?\}", "", llm_resp, flags=re.DOTALL).strip()
        user_histories[from_number].append({
            "role":"model","text":clean_text,"time":datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", clean_text)
        await send_typing_indicator(from_number)
        await send_whatsapp_message(from_number, clean_text)

        # ─── 10) Extraer intención de pedido y resumir ─────────────────────────
        order_data, _ = extract_order_data(llm_resp)
        if order_data and order_data.get("products"):
            # Calcular totales
            lineas = []
            subtotal = 0
            for item in order_data["products"]:
                nombre = item["name"]
                cantidad = int(item.get("quantity",1))
                prod = next((p for p in productos if p["name"]==nombre), None)
                precio_unit = prod["price_numeric"] if prod else 0
                total_item = precio_unit * cantidad
                subtotal += total_item
                lineas.append(f"{cantidad} x {nombre} (COP {precio_unit:,}) = COP {total_item:,}")
                item["price"] = precio_unit

            total = subtotal + 5000
            order_data["total"] = total

            # Enviar resumen
            resumen_text = "\n".join(lineas)
            await send_whatsapp_message(
                from_number,
                (
                    f"Aquí el resumen de tu pedido:\n"
                    f"{resumen_text}\n"
                    f"Subtotal: COP {subtotal:,}\n"
                    f"Envío: COP 5,000\n"
                    f"Total: COP {total:,}\n"
                    "¿Confirmas? 😊"
                )
)


            # Inicializar formulario si faltan datos
            user_pending_data[from_number] = order_data
            ctx["awaiting_fields"] = REQUIRED_FIELDS.copy()
        return

    except Exception:
        traceback.print_exc()
