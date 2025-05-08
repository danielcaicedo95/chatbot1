from datetime import datetime
import json
import re
import traceback
from difflib import get_close_matches

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import (
    send_whatsapp_message,
    send_whatsapp_image,
    send_typing_indicator  # Nuevo: simulador de "escribiendo..."
)
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order

# Campos obligatorios para confirmar pedido
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        # ─── 1) Depurar payload y extraer mensaje ───────────────────────────────
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages")
        if not messages:
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip().lower()
        from_number = msg.get("from")
        if not raw_text or not from_number:
            return

        # ─── 2) Guardar en historial y Supabase ─────────────────────────────────
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # ─── SIMULAR ESCRIBIENDO ───────────────────────────────────────────────
        await send_typing_indicator(from_number)

        # ─── 3) Cargar catálogo y filtrar precios inválidos ─────────────────────
        productos = await get_all_products()
        # Filtrar variantes/precios <= 0
        for p in productos:
            # Si precio principal inválido, saltar o marcar
            if p.get("price", 0) <= 0:
                p["price"] = "Consultar"
            for v in p.get("product_variants", []):
                if v.get("price", 0) <= 0:
                    v["price"] = "Consultar"

        # ─── 4) Detectar petición de imagen explícita ────────────────────────────
        wants_image = bool(re.search(r"\bfoto|imagen|muestra|ver\b", raw_text))
        if wants_image:
            # Encontrar producto o variante en texto
            target = raw_text
            chosen = None
            for p in productos:
                if p["name"].lower() in target:
                    chosen = (p, None)
                    break
                for v in p.get("product_variants", []):
                    if any(str(val).lower() in target for val in v.get("options", {}).values()):
                        chosen = (p, v)
                        break
                if chosen:
                    break

            if chosen:
                prod, var = chosen
                urls = []
                if var:
                    urls = [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") == var["id"]]
                if not urls:
                    urls = [img["url"] for img in prod.get("product_images", []) if img.get("variant_id") is None]

                # Enviar solo la imagen sin texto
                for u in urls:
                    await send_whatsapp_image(from_number, u)
                return
            else:
                await send_whatsapp_message(from_number, "Lo siento, no encontré esa imagen. ¿Puedes especificar el producto?")
                return

        # ─── 5) Flujo de conversación para ventas ────────────────────────────────
        # Construir contexto de catálogo legible
        contexto = []
        for p in productos:
            line = f"- {p['name']}: COP {p['price']}"
            variantes = p.get("product_variants", [])
            if variantes:
                opts = [f"{','.join(f'{k}:{v2}' for k,v2 in v['options'].items())} (COP {v['price']})" for v in variantes]
                line += " | Variantes: " + "; ".join(opts)
            contexto.append(line)
        catalogo_text = "Catálogo actual:\n" + "\n".join(contexto)

        await send_typing_indicator(from_number)
        instrucciones = (
            f"{raw_text}\n\n{catalogo_text}\n\n"
            "1️⃣ Si un producto no está disponible, sugiere uno alternativo.\n"
            "2️⃣ Si hay intención de compra, muestra resumen con subtotal + COP 5.000 envío y pregunta si desea algo más.\n"
            "3️⃣ Usa emojis y tono cercano.\n"
            "4️⃣ Al confirmar, solicita los siguientes datos: nombre, dirección, teléfono y método de pago."
        )
        hist = [m for m in user_histories[from_number] if m["role"] in ("user","model")]
        llm_resp = await ask_gemini_with_history(hist + [{"role": "user", "text": instrucciones}])

        # Limpiar posibles JSON en la respuesta
        clean_text = re.sub(r"\{.*?\}", "", llm_resp, flags=re.DOTALL).strip()
        user_histories[from_number].append({"role": "model", "text": clean_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_text)

        # Enviar respuesta de flujo
        await send_typing_indicator(from_number)
        await send_whatsapp_message(from_number, clean_text)

        # ─── 6) Extraer pedido y procesar ────────────────────────────────────
        from app.utils.extractors import extract_order_data
        order_data, _ = extract_order_data(llm_resp)
        if order_data and order_data.get("products"):
            # Pedir datos faltantes o confirmar
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
