 
import httpx
from app.config import WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID
from app.services.gemini import ask_gemini

async def handle_incoming_message(payload: dict):
    try:
        message = payload['entry'][0]['changes'][0]['value']['messages'][0]
        text = message['text']['body']
        from_number = message['from']

        # Llama a Gemini
        response = await ask_gemini(text)

        # Envía respuesta por WhatsApp
        await send_whatsapp_message(from_number, response)

    except Exception as e:
        print("Error procesando mensaje:", e)

async def send_whatsapp_message(to: str, message: str):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
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

    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, json=data)
