# app/clients/whatsapp.py

import requests
from app.core.config import WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID

def send_whatsapp_message(to: str, message: str):
    """Envía un mensaje de texto simple por WhatsApp."""
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
    try:
        resp = requests.post(url, headers=headers, json=data)
        resp.raise_for_status()
        print(f"✅ Texto enviado a {to}: {resp.status_code}")
    except requests.RequestException as e:
        print(f"❌ Error enviando texto a {to}: {e}")
        print("⚠️ Respuesta:", resp.text if 'resp' in locals() else 'No response')


def send_whatsapp_image(to: str, image_url: str, caption: str = None):
    """
    Envía una imagen por WhatsApp.
    - `image_url` debe ser una URL pública accesible (HTTPS).
    - `caption` es opcional.
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

    try:
        resp = requests.post(url, headers=headers, json=data)
        resp.raise_for_status()
        print(f"✅ Imagen enviada a {to}: {image_url}")
    except requests.RequestException as e:
        print(f"❌ Error enviando imagen a {to}: {e}")
        print("📸 URL:", image_url)
        print("⚠️ Respuesta:", resp.text if 'resp' in locals() else 'No response')
