from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
import requests
from collections import defaultdict, deque

from app.config import WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID
from app.services.gemini import ask_gemini_with_history  # ✅ Import actualizado

# Historial por usuario (memoria temporal en RAM)
user_histories = defaultdict(lambda: deque(maxlen=15))

router = APIRouter()
VERIFY_TOKEN = "gemini-bot-token"

@router.get("/webhook")
async def verify_webhook(hub_mode: str = None, hub_challenge: str = None, hub_verify_token: str = None):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge, status_code=200)
    return PlainTextResponse(content="Invalid verification token", status_code=403)

@router.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()
    print("Mensaje recibido:", body)

    try:
        entry = body['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        messages = value.get('messages')

        if messages:
            message = messages[0]
            text = message.get('text', {}).get('body')
            from_number = message.get('from')

            if text and from_number:
                # 🧠 Guardar mensaje del usuario en la memoria
                user_histories[from_number].append({"role": "user", "text": text})

                # Crear historial para enviar a Gemini
                history = [
                    {"parts": [{"text": m["text"]}]} for m in user_histories[from_number]
                ]

                # ✨ Preguntar a Gemini con el historial
                respuesta = await ask_gemini_with_history(history)

                # Guardar respuesta de Gemini en la memoria
                user_histories[from_number].append({"role": "assistant", "text": respuesta})

                # ✅ Enviar respuesta al usuario
                send_whatsapp_message(from_number, respuesta)
            else:
                print("Mensaje no contiene texto o número de origen válido.")
    except Exception as e:
        print("Error procesando el mensaje:", e)

    return {"status": "received"}

def send_whatsapp_message(to: str, message: str):
    url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {
            "body": message
        }
    }

    response = requests.post(url, headers=headers, json=data)
    print("Respuesta enviada:", response.status_code, response.text)
