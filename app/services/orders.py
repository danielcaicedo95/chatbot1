# app/services/orders.py

from app.services.supabase import supabase

async def create_order(phone: str, name: str, address: str, products: list, total: float):
    try:
        order_data = {
            "phone": phone,
            "name": name,
            "address": address,
            "products": products,
            "total": total
        }
        response = supabase.table("orders").insert(order_data).execute()
        print("📝 Pedido guardado en Supabase:", response)
        return response
    except Exception as e:
        print("❌ Error al guardar pedido:", e)
        return None
