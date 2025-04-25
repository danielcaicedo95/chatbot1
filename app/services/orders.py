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
    try:
        now = datetime.now(timezone.utc)
        five_minutes_ago = now - timedelta(minutes=5)

        existing_order = await get_recent_order_by_phone_number(phone, five_minutes_ago)

        # Preparamos datos según tu esquema (phone_number, no phone)
        order_data = {
            "phone_number": phone,
            "name": name,
            "address": address,
            "payment_method": payment_method,
            "products": products,
            "total": total
        }

        if existing_order:
            print("🔄 Pedido reciente encontrado. Actualizando...")
            order_id = existing_order.get("id")
            return await update_order_in_supabase(order_id, order_data)
        else:
            print("🆕 Pedido nuevo. Guardando...")
            # Ponemos created_at si quieres, pero Supabase ya lo asigna por defecto
            order_data["created_at"] = now.isoformat().replace("+00:00", "Z")
            return await save_order_to_supabase(order_data)

    except Exception as e:
        print("❌ Error al guardar pedido:", e)
        return None

# Alias para compatibilidad con conversation.py
update_order = create_order
