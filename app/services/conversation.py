# app/services/conversation.py

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase  # ↪️ Import agregado

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

        # 3) Generar respuesta
        history = list(user_histories[from_number])
        respuesta = await ask_gemini_with_history(history)

        # 4) Memoria RAM
        user_histories[from_number].append({"role": "model", "text": respuesta})
        # 5) Guardar en Supabase (bot)
        await save_message_to_supabase(from_number, "model", respuesta)

        # 6) Enviar respuesta al usuario
        send_whatsapp_message(from_number, respuesta)

    except Exception as e:
        print("Error procesando el mensaje:", e)
