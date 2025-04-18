from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message

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

            user_histories[from_number].append({"role": "user", "text": text})

            history = list(user_histories[from_number])

            respuesta = await ask_gemini_with_history(history)

            user_histories[from_number].append({"role": "model", "text": respuesta})

            send_whatsapp_message(from_number, respuesta)

    except Exception as e:
        print("Error procesando el mensaje:", e)
