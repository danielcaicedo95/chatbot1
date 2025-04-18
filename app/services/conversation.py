# app/services/conversation.py

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase  # ✅ Nuevo import

async def handle_user_message(body: dict):
    try:
        entry = body['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        messages = value.get('messages')

        if messages:
            msg = messages[0]
            text = msg.get('text', {}).get('body')
            from_number = msg.get('from')

            if not text or not from_number:
                print("Mensaje sin texto o número inválido.")
                return

            # 🧠 Guardamos el mensaje en memoria
            user_histories[from_number].append({"role": "user", "text": text})

            # 💾 Guardamos en Supabase (usuario)
            await save_message_to_supabase(from_number, "user", text)

            # 🤖 Preparamos el historial para Gemini
            history = list(user_histories[from_number])
            respuesta = await ask_gemini_with_history(history)

            # 🧠 Guardamos la respuesta en memoria
            user_histories[from_number].append({"role": "model", "text": respuesta})

            # 💾 Guardamos en Supabase (bot)
            await save_message_to_supabase(from_number, "bot", respuesta)

            # 📤 Enviamos la respuesta por WhatsApp
            send_whatsapp_message(from_number, respuesta)

    except Exception as e:
        print("Error procesando el mensaje:", e)
