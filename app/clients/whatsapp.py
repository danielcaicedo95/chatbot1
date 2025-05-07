# app/clients/whatsapp.py

import requests
from app.core.config import WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID

def send_whatsapp_message(to: str, message: str):
    """Envía un texto simple por WhatsApp."""
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
    resp = requests.post(url, headers=headers, json=data)
    print("Texto enviado:", resp.status_code, resp.text)


def send_whatsapp_image(to: str, image_url: str, caption: str = None):
    """
    Envía una imagen por WhatsApp.
    - `image_url` debe ser una URL pública accesible.
    - `caption` es opcional (texto que acompaña la imagen).
    """
    url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    image_payload = {"link": image_url}
    if caption:
        image_payload["caption"] = caption

    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": image_payload
    }
    resp = requests.post(url, headers=headers, json=data)
    print("Imagen enviada:", resp.status_code, resp.text)
