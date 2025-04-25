# app/services/orders.py
from datetime import datetime, timedelta, timezone
from app.services.supabase import (
    save_order_to_supabase,
    get_recent_order_by_phone_number,
    update_order_in_supabase
)

async def create_order(
    phone: str,
    name: str,
    address: str,
    products: list,
    total: float,
    payment_method: str
):
    """
    Crea o actualiza un pedido en Supabase.
    Si existe un pedido en los últimos 5 minutos para este `phone`, lo actualiza.
    """
    try:
        now = datetime.now(timezone.utc)
        five_minutes_ago = now - timedelta(minutes=5)
        
        existing = await get_recent_order_by_phone_number(phone, five_minutes_ago)

        payload = {
            "phone_number": phone,
            "name": name,
            "address": address,
            "payment_method": payment_method,
            "products": products,
            "total": total
        }

        if existing:
            print("🔄 Pedido reciente encontrado. Actualizando...")
            return await update_order_in_supabase(existing["id"], payload)
        else:
            print("🆕 Pedido nuevo. Guardando...")
            return await save_order_to_supabase(payload)

    except Exception as e:
        print("❌ Error al guardar pedido:", e)
        return None

# Alias para mantener compatibilidad con conversation.py
update_order = create_order
