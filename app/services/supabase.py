import httpx
from datetime import datetime
from app.core.config import SUPABASE_URL, SUPABASE_KEY


headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}


async def save_message_to_supabase(phone_number: str, role: str, text: str):
    url = f"{SUPABASE_URL}/rest/v1/messages"
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
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=order, headers=headers)
        print("📝 Pedido guardado en Supabase:", resp.status_code, resp.text)
        return resp.json()


async def get_recent_order_by_phone(phone: str, since_time: datetime):
    url = f"{SUPABASE_URL}/rest/v1/orders"
    query = f"?phone=eq.{phone}&created_at=gte.{since_time.isoformat()}&select=*"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url + query, headers=headers)
        data = resp.json()
        print("📦 Pedido reciente:", data)
        return data[0] if data else None


async def update_order_in_supabase(order_id: int, order_data: dict):
    url = f"{SUPABASE_URL}/rest/v1/orders?id=eq.{order_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.patch(url, json=order_data, headers=headers)
        print("✏️ Pedido actualizado en Supabase:", resp.status_code, resp.text)
        return resp.json()
