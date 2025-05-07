# app/services/orders.py

from datetime import datetime, timedelta, timezone

import httpx
from app.core.config import SUPABASE_URL, SUPABASE_KEY

from app.services.supabase import (
    save_order_to_supabase,
    get_recent_order_by_phone_number,
    update_order_in_supabase
)
from app.services.products import update_product_stock
from app.utils.memory import user_orders, user_pending_data
from app.utils.validators import get_missing_fields, REQUIRED_FIELDS

# Cabeceras para Supabase REST
_order_headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

async def get_all_orders():
    """
    Retorna todas las órdenes, ordenadas por `created_at` descendente.
    """
    url = f"{SUPABASE_URL}/rest/v1/orders?select=*&order=created_at.desc"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=_order_headers)
        resp.raise_for_status()
        return resp.json()


async def create_order(
    phone: str,
    name: str,
    address: str,
    products: list,
    total: float,
    payment_method: str
):
    """
    Crea un pedido nuevo o actualiza uno existente en Supabase.
    """
    try:
        now = datetime.now(timezone.utc)
        five_minutes_ago = now - timedelta(minutes=5)

        existing_order = await get_recent_order_by_phone_number(phone, five_minutes_ago)

        order_payload = {
            "phone_number": phone,
            "name": name,
            "address": address,
            "payment_method": payment_method,
            "products": products,
            "total": total,
        }

        if existing_order:
            # Actualizar pedido reciente
            order_id = existing_order.get("id")
            return await update_order_in_supabase(order_id, order_payload)
        else:
            # Crear pedido nuevo
            order_payload["created_at"] = now.isoformat().replace("+00:00", "Z")
            return await save_order_to_supabase(order_payload)

    except Exception as e:
        print("❌ Error al guardar pedido:", e)
        return None

# Alias para compatibilidad con conversation.py
update_order = create_order


async def process_order(from_number: str, order_data: dict) -> dict:
    """
    Fusiona `order_data` con el estado pendiente, valida campos, decide crear/actualizar,
    descuenta stock y limpia el estado.

    Devuelve un dict con:
      - status: "missing" / "created" / "updated" / "error"
      - fields: lista de campos faltantes (solo si status == "missing")
      - response: resultado crudo de Supabase (para created/updated)
    """
    # 1) Fusionar sólo valores explícitos (evita placeholders)
    pending = user_pending_data.get(from_number, {})
    for key, value in order_data.items():
        if isinstance(value, str) and value.strip().lower().startswith("tu "):
            continue
        if value is not None:
            pending[key] = value
    user_pending_data[from_number] = pending

    # 2) Convertir placeholders tipo "tu ..." en None
    for field in REQUIRED_FIELDS:
        v = pending.get(field, "")
        if isinstance(v, str) and v.strip().lower().startswith("tu "):
            pending[field] = None

    # 3) Validar datos obligatorios
    faltantes = get_missing_fields(pending)
    if faltantes:
        return {"status": "missing", "fields": faltantes}

    # 4) Decide crear o actualizar
    now = datetime.now(timezone.utc)
    five_minutes_ago = now - timedelta(minutes=5)
    existing = await get_recent_order_by_phone_number(pending["phone"], five_minutes_ago)

    # Prepara payload para Supabase
    supabase_payload = {
        "phone_number": pending["phone"],
        "name": pending["name"],
        "address": pending["address"],
        "payment_method": pending["payment_method"],
        "products": pending["products"],
        "total": pending["total"],
        "created_at": now.isoformat().replace("+00:00", "Z")
    }

    if existing:
        res = await update_order_in_supabase(existing["id"], supabase_payload)
        action = "updated"
    else:
        res = await save_order_to_supabase(supabase_payload)
        action = "created"

    if not res or not res.get("id"):
        return {"status": "error"}

    # 5) Descontar stock y limpiar estado pendiente
    for prod in pending["products"]:
        await update_product_stock(prod["name"], prod["quantity"])

    user_orders[from_number] = {"id": res["id"], "timestamp": now}
    user_pending_data.pop(from_number, None)

    return {"status": action, "response": res}
