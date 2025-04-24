# app/services/conversation.py

from app.utils.memory import user_histories, user_orders
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products
from datetime import datetime
from app.services.orders import create_order


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

        # — 1) Guardar mensaje del usuario —
        user_histories.setdefault(from_number, []).append({"role": "user", "text": text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "user", text)

        # — 2) Primer contacto: enviar Hook inicial —
        if len(user_histories[from_number]) == 1:
            greeting = (
                "¡Hola! 👋 Soy el asistente de Licores El Roble. "
                "¿En qué puedo ayudarte hoy? "
                "¿Te gustaría ver nuestros productos, hacer una consulta o realizar una compra? 🍷"
            )
            user_histories[from_number].append({"role": "model", "text": greeting, "time": datetime.utcnow().isoformat()})
            await save_message_to_supabase(from_number, "model", greeting)
            send_whatsapp_message(from_number, greeting)
            return

        # — 3) Construir contexto real de catálogo —
        productos = await get_all_products()
        contexto = "Catálogo actual:\n"
        for p in productos:
            size = p.get("size", "botella estándar")
            contexto += f"- {p['name']} ({size}): ${p['price']}\n"

        # — 4) Preparar prompt para Gemini con embudo de ventas —
        #   Le damos a Gemini: historial + contexto + instrucciones de flujo
        #   para que actúe como un vendedor humano siguiendo los 7 pasos.
        user_message = (
            f"{text}\n\n"
            f"{contexto}\n"
            "Instrucciones de venta:\n"
            "1. Engancha con un mensaje amistoso.\n"
            "2. Pregunta si quieren ver productos, resolver dudas o comprar.\n"
            "3. Muestra productos populares o promos.\n"
            "4. Añade urgencia o prueba social.\n"
            "5. Llama a la acción suave: ¿reservamos uno?\n"
            "6. Si aceptan, pide en orden: nombre, teléfono, dirección, pago.\n"
            "7. Confirma pedido y ofrece seguimiento.\n\n"
            "Responde de forma totalmente humana, con emojis, sin mostrar datos técnicos "
            "(stock, IDs, etc.). Sé amable, breve y directo."
        )

        # Reemplazamos el último mensaje "user" por este enriquecido
        user_histories[from_number][-1] = {"role": "user", "text": user_message, "time": datetime.utcnow().isoformat()}

        # — 5) Llamar a Gemini con el contexto completo —
        history = user_histories[from_number]
        respuesta = await ask_gemini_with_history(history)

        # — 6) Guardar respuesta y enviarla —
        user_histories[from_number].append({"role": "model", "text": respuesta, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", respuesta)
        send_whatsapp_message(from_number, respuesta)

        # — 7) Si Gemini considera que ya hay pedido, lo confirmamos aquí —
        #    (Detectamos si en el texto de Gemini aparece "Tu pedido ha sido registrado" o similar)
        if "pedido" in respuesta.lower() and ("registrado" in respuesta.lower() or "confirmado" in respuesta.lower()):
            # Aquí podrías llamar a create_order() con datos que Gemini le pida al usuario
            # Una implementación simple:
            state = user_orders.get(from_number, {})
            if state.get("productos"):
                await create_order(
                    from_number,
                    state.get("user_name", ""),
                    state.get("user_address", ""),
                    state.get("telefono", ""),
                    state.get("productos", []),
                    state.get("total_amount", 0.0)
                )
                # Limpiar estado
                user_orders.pop(from_number, None)

    except Exception as e:
        print("Error procesando el mensaje:", e)
