# app/services/orders.py

from app.services.supabase import save_order_to_supabase


async def create_order(phone: str, name: str, address: str, products: list, total: float):
    try:
        order_data = {
            "phone": phone,
            "name": name,
            "address": address,
            "products": products,
            "total": total
        }
        response = await save_order_to_supabase(order_data)
        return response
    except Exception as e:
        print("❌ Error al guardar pedido:", e)
        return None
