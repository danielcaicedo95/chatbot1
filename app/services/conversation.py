from datetime import datetime, timedelta
import json

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order

# Campos obligatorios para confirmar pedido (si los necesitas en este módulo, sino déjalos para validators)
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        entry = body['entry'][0]
        changes = entry['changes'][0]
        messages = changes['value'].get('messages')
        if not messages:
            return

        msg = messages[0]
        text = msg.get('text', {}).get('body', '').strip()
        from_number = msg.get('from')
        if not text or not from_number:
            return

        # 1) Guardar mensaje en historial y Supabase
        user_histories.setdefault(from_number, []).append({
            "role": "user", "text": text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", text)

        # 2) Primer saludo
        if len(user_histories[from_number]) == 1:
            saludo = (
                "¡Hola! 👋 Soy el asistente de *Licores El Roble*. "
                "¿Quieres ver nuestro catálogo, resolver alguna duda o hacer un pedido? 🍻"
            )
            user_histories[from_number].append({
                "role": "model", "text": saludo,
                "time": datetime.utcnow().isoformat()
            })
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # 3) Obtener catálogo y armar prompt
        productos = await get_all_products()
        contexto = "Catálogo actual:\n" + "\n".join(
            f"- {p['name']} ({p.get('size', 'botella estándar')}): ${p['price']}" \
            for p in productos
        )

        instrucciones = (
            f"{text}\n\n"
            f"{contexto}\n"
            "INSTRUCCIONES para el asistente:\n"
            "1. Si algún producto no está disponible, sugiere una alternativa similar.\n"
            "2. Al detectar intención de compra, responde con:\n"
            "   - Lista de productos con cantidades y precios\n"
            "   - Subtotal + $5000 de envío\n"
            "   - ¿Deseas algo más?\n"
            "   - Recomienda 1 producto adicional para acompañar (guayabo, snacks, etc.)\n"
            "   - Si dice “no”, pide datos (nombre, dirección, teléfono, pago).\n"
            "3. Incluye emojis y tono humano.\n"
            "4. Al confirmar, añade al final este JSON:\n"
            "{\"order_details\":{\"name\":\"NOMBRE\",\"address\":\"DIRECCIÓN\",\"phone\":\"TELÉFONO\",\"payment_method\":\"TIPO_PAGO\",\"products\":[{\"name\":\"NOMBRE\",\"quantity\":1,\"price\":0}],\"total\":0}}\n"
            "Si modifica dentro de 5 min, actualiza el pedido.\n"
            "Responde como un amigo. 😄"
        )

        # Reemplazar el último mensaje en historial
        user_histories[from_number][-1]["text"] = instrucciones
        gemini_resp = await ask_gemini_with_history(user_histories[from_number])

        # 4) Extraer JSON de pedido y limpiar texto
        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(gemini_resp)

        # Guardar respuesta limpia
        user_histories[from_number].append({
            "role": "model", "text": clean_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", clean_text)

        # 🔍 Recomendaciones basadas en el pedido parcial
        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(
                    f"- {r['name']} ({r.get('size', 'estándar')}): ${r['price']}" \
                    for r in recomendaciones
                )
                clean_text += (
                    "\n🧠 Basado en tu pedido, podrías acompañarlo con:\n" +
                    texto_rec +
                    "\n¿Te gustaría agregar alguno de estos?"
                )

        send_whatsapp_message(from_number, clean_text)

        # 5) Procesar pedido con lógica centralizada en orders.py
        if order_data:
            result = await process_order(from_number, order_data)
            status = result.get("status")
            if status == "missing":
                campos = "\n".join(f"- {f.replace('_',' ')}" for f in result.get("fields", []))
                send_whatsapp_message(from_number, f"📋 Para completar tu pedido necesito:\n{campos}")
            elif status == "created":
                send_whatsapp_message(from_number, "✅ ¡Tu pedido ha sido confirmado! Gracias 🥳")
            elif status == "updated":
                send_whatsapp_message(from_number, "♻️ Pedido actualizado y stock descontado correctamente.")
            else:
                send_whatsapp_message(from_number, "❌ Ocurrió un error guardando tu pedido.")

    except Exception as e:
        print("❌ Error procesando mensaje:", e)
