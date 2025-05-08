from datetime import datetime
import json
import re
import traceback

from difflib import get_close_matches
from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import (
    send_whatsapp_message,
    send_whatsapp_image
)
# Intentar importar indicador de escritura; si no existe, usar stub
try:
    from app.clients.whatsapp import send_typing_indicator
except ImportError:
    async def send_typing_indicator(_):
        return

from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order

# Campos obligatorios para confirmar pedido
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        # 1) Parseo de payload
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
        normalized = raw_text.lower()

        # 2) Guardar historial y Supabase
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # 3) Simular "escribiendo..."
        await send_typing_indicator(from_number)

        # 4) Cargar catálogo y sanitizar precios/stock
        productos = await get_all_products()
        for p in productos:
            if p.get("price", 0) <= 0:
                p["price"] = "Consultar"
            if p.get("stock", 0) <= 0:
                p["stock"] = "Agotado"
            for v in p.get("product_variants", []):
                if v.get("price", 0) <= 0:
                    v["price"] = "Consultar"
                if v.get("stock", 0) <= 0:
                    v["stock"] = "Agotado"

        # 5) Manejo multimedia (envío de imágenes)
        if re.search(r"\b(foto|imagen|muestra|ver)\b", normalized):
            selected = None
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
                await send_whatsapp_message(from_number, "Lo siento, no encontré esa imagen. ¿Puedes darme el nombre exacto del producto o variante?")
                return

            prod, var = selected
            urls = []
            if var:
                urls = [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") == var["id"]]
            if not urls:
                urls = [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") is None]

            if not urls:
                await send_whatsapp_message(from_number, "Lo siento, no tengo imágenes disponibles para eso.")
                return

            # Enviar sólo imágenes sin texto
            for url in urls:
                await send_whatsapp_image(from_number, url)
            return

        # 6) Construir contexto de catálogo para LLM
        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: COP {p['price']} (stock {p['stock']})"
            variantes = p.get("product_variants") or []
            if variantes:
                opts = []
                for v in variantes:
                    opt_label = ",".join(f"{k}:{v2}" for k, v2 in v["options"].items())
                    opts.append(f"{opt_label} (stock {v['stock']})")
                line += " | Variantes: " + "; ".join(opts)
            imgs = p.get("product_images") or []
            if imgs:
                line += f" | Imágenes: {len(imgs)}"
            contexto_lines.append(line)
        contexto = "Catálogo actual:\n" + "\n".join(contexto_lines)
        print("DEBUG - Contexto construido:\n", contexto)

        # 7) Llamada a LLM para flujo de pedidos
        await send_typing_indicator(from_number)
        instrucciones = (
            f"{raw_text}\n\n{contexto}\n\n"
            "INSTRUCCIONES:\n"
            "1. Si un producto no está disponible, sugiere uno alternativo de forma persuasiva.\n"
            "2. Si detectas intención de compra, detalla productos, cantidad y precios, luego subtotal + COP 5.000 envío y pregunta si desea algo más.\n"
            "3. Recomienda 1 producto adicional basado en el carrito.\n"
            "4. Usa emojis y tono humano.\n"
            "5. Al confirmar, solicita: nombre, dirección, teléfono y método de pago.\n"
            "6. NO incluyas JSON en tu respuesta; al final guarda la orden usando JSON oculto."
        )
        hist = [m for m in user_histories[from_number] if m["role"] in ("user", "model")]
        llm_resp = await ask_gemini_with_history(hist + [{"role": "user", "text": instrucciones}])
        print("DEBUG - LLM order flow:\n", llm_resp)

        # 8) Limpiar cualquier JSON residual y almacenar respuesta
        clean_text = re.sub(r"\{.*?\}", "", llm_resp, flags=re.DOTALL).strip()
        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)

        # 9) Enviar respuesta de flujo
        await send_typing_indicator(from_number)
        await send_whatsapp_message(from_number, clean_text)

        # 10) Extraer datos de orden y recomendaciones
        from app.utils.extractors import extract_order_data
        order_data, _ = extract_order_data(llm_resp)
        if order_data and order_data.get("products"):
            # Sugerir productos adicionales
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
                await send_whatsapp_message(
                    from_number,
                    f"🧠 Podrías acompañar tu pedido con:\n{texto_rec}\n¿Te interesa alguno?"
                )

            # Procesar la orden (crear o actualizar en DB)
            result = await process_order(from_number, order_data)
            status = result.get("status")
            if status == "missing":
                faltantes = result.get("fields", [])
                campos = "\n".join(f"- {f.replace('_',' ')}" for f in faltantes)
                await send_whatsapp_message(from_number, f"📋 Por favor completa:\n{campos}")
            elif status == "created":
                await send_whatsapp_message(from_number, "✅ Pedido confirmado. ¡Gracias! 🎉")
            elif status == "updated":
                await send_whatsapp_message(from_number, "♻️ Pedido actualizado correctamente.")
            else:
                await send_whatsapp_message(from_number, "❌ Error guardando el pedido.")
        return

    except Exception:
        print("ERROR in handle_user_message:\n", traceback.format_exc())
