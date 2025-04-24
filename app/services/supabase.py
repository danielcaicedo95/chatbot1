# app/services/supabase.py

import httpx
from datetime import datetime
from app.core.config import SUPABASE_URL, SUPABASE_KEY


async def save_message_to_supabase(phone_number: str, role: str, text: str):
    url = f"{SUPABASE_URL}/rest/v1/messages"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    payload = {
        "phone_number": phone_number,
        "role": role,
        "text": text,
        "timestamp": datetime.utcnow().isoformat()
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        print("Mensaje guardado en Supabase:", resp.status_code, resp.text)


async def save_order_to_supabase(order: dict):
    url = f"{SUPABASE_URL}/rest/v1/orders"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=order, headers=headers)
        print("📝 Pedido guardado en Supabase:", resp.status_code, resp.text)
        return resp
