# app/routes/webhook.py
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
import requests
from collections import defaultdict, deque

from app.config import WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID
from app.services.gemini import ask_gemini_with_history  # Import actualizado

# Memoria temporal en RAM: guarda hasta 15 textos por usuario
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
                # 1) Guardar el texto entrante en la memoria
                user_histories[from_number].append(text)

                # 2) Convertir la memoria en lista de textos
                history_texts = list(user_histories[from_number])

                # 3) Consultar a Gemini con todo el historial
                respuesta = await ask_gemini_with_history(history_texts)

                # 4) Guardar la respuesta en la memoria
                user_histories[from_number].append(respuesta)

                # 5) Enviar la respuesta por WhatsApp
                send_whatsapp_message(from_number, respuesta)
            else:
                print("Mensaje sin texto o número inválido.")
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
        "text": {"body": message}
    }
    response = requests.post(url, headers=headers, json=data)
    print("Respuesta enviada:", response.status_code, response.text)
