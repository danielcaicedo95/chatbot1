from datetime import datetime
import json
import re
import traceback

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
        # ─── 1) Extraer y depurar payload ─────────────────────────────────────
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages", [])
        if not messages:
            return
        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        if not raw_text or not from_number:
            return

        # ─── 2) Guardar en historial y Supabase ────────────────────────────────
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # ─── 3) Cargar catálogo completo ───────────────────────────────────────
        productos = await get_all_products()
        # Reconstruir catálogo con validaciones de precio/stock
        catalog = []
        for p in productos:
            base_price = p.get("price", 0)
            base_stock = p.get("stock", 0)
            # evitar precio o stock negativo o cero en base
            if base_price <= 0:
                base_price = 0.01
            if base_stock < 0:
                base_stock = 0

            variants = []
            for v in p.get("product_variants", []):
                # validar override
                v_price = v.get("price", base_price) or base_price
                v_stock = v.get("stock", base_stock) if v.get("stock", base_stock) >= 0 else base_stock
                opts = v.get("options", {})
                # construir valor y etiqueta
                value = next(iter(opts.values())).lower() if opts else ""
                label = v.get("variant_label") or ",".join(f"{k}:{opts[k]}" for k in opts)
                imgs = [img["url"] for img in p.get("product_images", []) if img.get("variant_id") == v.get("id")]
                variants.append({
                    "id": v.get("id"),
                    "value": value,
                    "label": label,
                    "price": v_price,
                    "stock": v_stock,
                    "images": imgs
                })
            # imágenes generales
            main_imgs = [img["url"] for img in p.get("product_images", []) if img.get("variant_id") is None]

            catalog.append({
                "id": p.get("id"),
                "name": p.get("name"),
                "price": base_price,
                "stock": base_stock,
                "variants": variants,
                "images": main_imgs
            })

        # ─── 4) Preparar prompt para Gemini ────────────────────────────────────
        history = user_histories[from_number][-10:]
        prompt = {
            "history": history,
            "catalog": catalog,
            "instructions": [
                "Detecta si el usuario quiere ver imágenes de un producto o variante.",
                "Si pide foto, responde con {type: 'images', urls: [...], caption: 'texto opcional'} y no incluyas texto adicional al enviar en WhatsApp.",
                "Para interacción de venta, responde con {type: 'text', content: 'mensaje'} y fluye como un humano, sin JSON en WhatsApp.",
                "Sugiere productos, calcula totales, ofrece envío de COP 5.000, recomienda un adicional, y al final recoge datos de pedido sin usar json visibles al usuario."
            ]
        }
        llm_input = history + [{"role": "user", "text": json.dumps(prompt, ensure_ascii=False)}]
        llm_resp = await ask_gemini_with_history(llm_input)

        # ─── 5) Parsear respuesta de Gemini ───────────────────────────────────
        try:
            resp_obj = json.loads(llm_resp)
        except Exception:
            # fallback a texto libre
            resp_obj = {"type": "text", "content": llm_resp}

        # ─── 6) Enviar multimedia si corresponde ──────────────────────────────
        if resp_obj.get("type") == "images":
            urls = resp_obj.get("urls", [])[:10]
            # solo imágenes, sin captions
            for url in urls:
                await send_whatsapp_image(from_number, url)
            return

        # ─── 7) Procesar mensaje de texto ─────────────────────────────────────
        user_message = resp_obj.get("content", "")
        # Guardar respuesta parcial en historial
        user_histories[from_number].append({"role": "model", "text": user_message, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", user_message)

        # ─── 8) Flujo de pedido con extractor ─────────────────────────────────
        from app.utils.extractors import extract_order_data
        # Extraer datos si el usuario proporcionó info de pedido
        order_data, clean_text = extract_order_data(user_message)

        if not order_data or not order_data.get("products"):
            # solo enviar texto si no hay pedido completo
            await send_whatsapp_message(from_number, user_message)
            return

        # ─── 9) Obtener recomendaciones de productos adicionales ─────────────
        recomendaciones = await get_recommended_products(order_data.get("products", []))
        if recomendaciones:
            texto_rec = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
            await send_whatsapp_message(from_number, f"🧠 Podrías acompañar tu pedido con:\n{texto_rec}\n¿Te interesa alguno?")
            # esperar respuesta de usuario antes de continuar
            return

        # ─── 10) Guardar o actualizar pedido ──────────────────────────────────
        result = await process_order(from_number, order_data)
        status = result.get("status")
        if status == "missing":
            campos = "\n".join(f"- {f.replace('_', ' ')}" for f in result.get("fields", []))
            await send_whatsapp_message(from_number, f"📋 Faltan datos:\n{campos}")
        elif status == "created":
            await send_whatsapp_message(from_number, "✅ Pedido confirmado. ¡Gracias! 🎉")
        elif status == "updated":
            await send_whatsapp_message(from_number, "♻️ Pedido actualizado correctamente.")
        else:
            await send_whatsapp_message(from_number, "❌ Error guardando el pedido.")

    except Exception:
        print("❌ [ERROR] in handle_user_message:", traceback.format_exc())
