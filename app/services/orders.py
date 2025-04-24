from datetime import datetime, timedelta, timezone
from app.services.supabase import save_order_to_supabase, get_recent_order_by_phone, update_order_in_supabase


async def create_order(phone: str, name: str, address: str, products: list, total: float, payment_method: str):
    try:
        now = datetime.now(timezone.utc)
        five_minutes_ago = now - timedelta(minutes=5)

        existing_order = await get_recent_order_by_phone(phone, five_minutes_ago)

        order_data = {
            "phone": phone,
            "name": name,
            "address": address,
            "products": products,
            "total": total,
            "payment_method": payment_method,
            "updated_at": now.isoformat()
        }

        if existing_order:
            print("🔄 Pedido reciente encontrado. Actualizando...")
            order_id = existing_order.get("id")
            return await update_order_in_supabase(order_id, order_data)
        else:
            print("🆕 Pedido nuevo. Guardando...")
            order_data["created_at"] = now.isoformat()
            return await save_order_to_supabase(order_data)
    except Exception as e:
        print("❌ Error al guardar pedido:", e)
        return None


# Alias para mantener compatibilidad si se usa `update_order` desde conversation.py
update_order = create_order
