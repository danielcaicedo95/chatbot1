# app/services/supabase.py
import httpx
from datetime import datetime
from app.core.config import SUPABASE_URL, SUPABASE_KEY
import uuid
from typing import Tuple

# Cabeceras globales para Supabase
headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def utc_iso_z():
    # Devuelve timestamp en formato ISO 8601 UTC con 'Z' (Zulu)
    return datetime.utcnow().isoformat() + "Z"

async def save_message_to_supabase(phone_number: str, role: str, text: str):
    url = f"{SUPABASE_URL}/rest/v1/messages"
    payload = {
        "phone_number": phone_number,
        "role": role,
        "text": text,
        "timestamp": utc_iso_z()
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        print("Mensaje guardado en Supabase:", resp.status_code, resp.text)

async def save_order_to_supabase(order: dict):
    """
    Inserta un nuevo pedido en la tabla `orders`.
    Retorna el registro insertado o None si falla.
    """
    url = f"{SUPABASE_URL}/rest/v1/orders"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=order, headers=headers)
        print("📝 Pedido guardado en Supabase:", resp.status_code, resp.text)
        data = resp.json()
        return data[0] if isinstance(data, list) and data else None

async def get_recent_order_by_phone_number(phone_number: str, since_time: datetime):
    """
    Busca un pedido por número de teléfono creado desde `since_time` hasta ahora.
    Retorna el primer pedido encontrado o None.
    """
    # Convertimos since_time a ISO con 'Z'
    since = since_time.isoformat().replace("+00:00", "Z")
    url = f"{SUPABASE_URL}/rest/v1/orders"
    query = f"?phone_number=eq.{phone_number}&created_at=gte.{since}&select=*"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url + query, headers=headers)
        data = resp.json()
        print("📦 Pedido reciente:", data)
        return data[0] if data else None

async def update_order_in_supabase(order_id: str, order_data: dict):
    """
    Actualiza un pedido existente dado su `id`.
    Retorna el registro actualizado o None.
    """
    url = f"{SUPABASE_URL}/rest/v1/orders?id=eq.{order_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.patch(url, json=order_data, headers=headers)
        print("✏️ Pedido actualizado en Supabase:", resp.status_code, resp.text)
        data = resp.json()
        return data[0] if isinstance(data, list) and data else None




async def upload_image_to_supabase_storage(file_data: bytes, filename: str, content_type: str) -> Tuple[bool, str]:
    """
    Sube una imagen a Supabase Storage (bucket: 'product-images').
    Retorna (True, url) si fue exitoso, o (False, mensaje de error) si falló.
    """
    # Crear nombre único en el bucket
    ext = filename.split(".")[-1]
    unique_filename = f"{uuid.uuid4()}.{ext}"
    path = f"products/{unique_filename}"
    
    url = f"{SUPABASE_URL}/storage/v1/object/product-images/{path}"
    headers_upload = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, content=file_data, headers=headers_upload)

        if resp.status_code == 200:
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/product-images/{path}"
            return True, public_url
        else:
            print("❌ Error subiendo imagen:", resp.status_code, resp.text)
            return False, resp.text
