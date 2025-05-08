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
        # ─── 1) Parseo de payload ──────────────────────────────────────────────
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages", [])
        if not messages:
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip().lower()
        from_number = msg.get("from")
        if not raw_text or not from_number:
            return

        # ─── 2) Guardar historial y Supabase ───────────────────────────────────
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # ─── 3) Simular "escribiendo..." ───────────────────────────────────────
        await send_typing_indicator(from_number)

        # ─── 4) Cargar catálogo y sanitizar precios ────────────────────────────
        productos = await get_all_products()
        for p in productos:
            if p.get("price", 0) <= 0:
                p["price"] = "Consultar"
            for v in p.get("product_variants", []):
                if v.get("price", 0) <= 0:
                    v["price"] = "Consultar"

        # ─── 5) Manejo multimedia (fotos) ─────────────────────────────────────
        wants_image = bool(re.search(r"\b(foto|imagen|muestra|ver)\b", raw_text))
        if wants_image:
            # Buscar producto o variante
            selected = None
            for p in productos:
                if p["name"].lower() in raw_text:
                    selected = (p, None)
                    break
                for v in p.get("product_variants", []):
                    if any(str(val).lower() in raw_text for val in v.get("options", {}).values()):
                        selected = (p, v)
                        break
                if selected:
                    break

            if not selected:
                await send_whatsapp_message(from_number, "Lo siento, no encontré esa imagen. ¿Puedes decirme el nombre exacto?")
                return

            prod, var = selected
            # Obtener URLs
            if var:
                urls = [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") == var["id"]]
            else:
                urls = [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") is None]

            if not urls:
                await send_whatsapp_message(from_number, "Lo siento, no tengo imágenes disponibles para eso.")
                return

            # Enviar solo imágenes, sin texto
            for url in urls:
                await send_whatsapp_image(from_number, url)
            return

        # ─── 6) Construcción de contexto para venta ────────────────────────────
        contexto = [
            f"- {p['name']}: COP {p['price']}" + (
                " | Variantes: " + "; ".join(
                    f"{','.join(f'{k}:{v2}' for k,v2 in variant['options'].items())} (COP {variant['price']})"
                    for variant in p.get("product_variants", [])
                ) if p.get("product_variants") else ""
            )
            for p in productos
        ]
        catalogo_text = "Catálogo actual:\n" + "\n".join(contexto)

        # ─── 7) Llamada a LLM para flujo de venta ──────────────────────────────
        await send_typing_indicator(from_number)
        instrucciones = (
            f"{raw_text}\n\n{catalogo_text}\n\n"
            "1️⃣ Si un producto está agotado, sugiere un alternativo.\n"
            "2️⃣ Si hay intención de compra, muestra subtotales (+ COP 5.000 envío), pregunta si desea algo más.\n"
            "3️⃣ Usa emojis y tono humano.\n"
            "4️⃣ Al confirmar, pide datos: nombre, dirección, teléfono y método de pago."
        )
        hist = [m for m in user_histories[from_number] if m["role"] in ("user", "model")]
        llm_resp = await ask_gemini_with_history(hist + [{"role": "user", "text": instrucciones}])

        # ─── 8) Limpiar JSON residual y guardar respuesta ───────────────────────
        clean_text = re.sub(r"\{.*?\}", "", llm_resp, flags=re.DOTALL).strip()
        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)

        # ─── 9) Enviar respuesta de conversación ──────────────────────────────
        await send_typing_indicator(from_number)
        await send_whatsapp_message(from_number, clean_text)

        # ─── 10) Extracción y procesamiento de orden ──────────────────────────
        from app.utils.extractors import extract_order_data
        order_data, _ = extract_order_data(llm_resp)
        if order_data and order_data.get("products"):
            # Procesar o solicitar campos faltantes
            result = await process_order(from_number, order_data)
            if result.get("status") == "missing":
                faltantes = result.get("fields", [])
                campos = "\n".join(f"- {f.replace('_',' ')}" for f in faltantes)
                await send_whatsapp_message(from_number, f"📋 Por favor completa:\n{campos}")
            else:
                await send_whatsapp_message(from_number, "✅ Pedido procesado exitosamente. ¡Gracias! 🎉")
        return

    except Exception:
        traceback.print_exc()
