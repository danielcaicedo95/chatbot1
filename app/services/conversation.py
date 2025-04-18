# app/services/conversation.py

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import search_products_by_keyword


async def handle_user_message(body: dict):
    try:
        entry = body['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        messages = value.get('messages')

        if not messages:
            return

        msg = messages[0]
        text = msg.get('text', {}).get('body')
        from_number = msg.get('from')

        if not text or not from_number:
            print("Mensaje sin texto o número inválido.")
            return

        # 1) Memoria RAM
        user_histories[from_number].append({"role": "user", "text": text})

        # 2) Guardar en Supabase (usuario)
        await save_message_to_supabase(from_number, "user", text)

        # 🔍 3) Buscar productos relacionados con el mensaje
        productos = await search_products_by_keyword(text)
        print("🔍 Resultado productos:", productos)


        # 📦 4) Si hay productos, formatearlos como contexto adicional
        if productos:
            productos_texto = "🛍️ Productos relacionados con lo que preguntaste:\n\n"
            for prod in productos:
                productos_texto += f"- {prod['name']}: {prod['description']}. Precio: ${prod['price']}. Stock: {prod['stock']}\n"

            print("📦 Texto final con productos:", productos_texto)

            user_histories[from_number].append({
                    "role": "user",
                    "text": f"(Contexto del sistema para ayudarte): {productos_texto}"
                })



        # 5) Generar respuesta de Gemini con historial actualizado
        history = list(user_histories[from_number])
        respuesta = await ask_gemini_with_history(history)

        # 6) Memoria RAM
        user_histories[from_number].append({"role": "model", "text": respuesta})

        # 7) Guardar en Supabase (respuesta del bot)
        await save_message_to_supabase(from_number, "model", respuesta)

        # 8) Enviar respuesta por WhatsApp
        send_whatsapp_message(from_number, respuesta)

    except Exception as e:
        print("Error procesando el mensaje:", e)
