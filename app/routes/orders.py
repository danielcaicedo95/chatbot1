# app/routes/orders.py
from fastapi import APIRouter, HTTPException
from typing import List

from app.services.orders import get_all_orders, create_order  # reusa create_order si quieres exponerlo aquí

router = APIRouter(prefix="/orders", tags=["orders"])

@router.get("/", summary="List all orders")
async def list_orders():
    try:
        return await get_all_orders()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching orders: {e}")

@router.delete("/{order_id}", summary="Delete an order")
async def delete_order(order_id: str):
    """
    (Opcional) Borra la orden indicada. Úsalo si alguna vez quieres limpiar ventas antiguas.
    """
    from app.services.supabase import headers, SUPABASE_URL
    import httpx

    url = f"{SUPABASE_URL}/rest/v1/orders?id=eq.{order_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)
        if resp.status_code in (200, 204):
            return {"message": "Order deleted"}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
