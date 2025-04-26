import os
import httpx
from app.core.config import SUPABASE_URL, SUPABASE_KEY

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

async def get_all_products():
    url = f"{SUPABASE_URL}/rest/v1/products?select=*"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        return resp.json()

async def get_product_by_id(product_id: str):
    url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}&select=*"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        return resp.json()

async def create_product(data: dict):
    url = f"{SUPABASE_URL}/rest/v1/products"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers={**headers, "Prefer": "return=representation"}, json=data)
        return resp.json()

async def search_products_by_keyword(keyword: str):
    url = f"{SUPABASE_URL}/rest/v1/products?name=ilike.*{keyword}*&select=*"
    print("📦 Buscando productos con URL:", url)

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        print("📦 Respuesta Supabase:", resp.status_code, resp.text)

        if resp.status_code != 200:
            print("❌ Error al buscar productos.")
            return []

        data = resp.json()
        print("📦 Productos encontrados:", data)
        return data

async def update_product_stock(product_name: str, quantity_sold: int):
    # Obtener producto por nombre
    url = f"{SUPABASE_URL}/rest/v1/products?name=eq.{product_name}&select=*"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200 or not resp.json():
            print(f"❌ Producto '{product_name}' no encontrado en inventario.")
            return

        product = resp.json()[0]
        product_id = product["id"]
        current_stock = product.get("stock", 0)

        # Restar la cantidad vendida
        new_stock = max(0, current_stock - quantity_sold)

        # Actualizar el stock en Supabase
        patch_url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}"
        patch_data = {"stock": new_stock}

        patch_resp = await client.patch(patch_url, headers=headers, json=patch_data)
        if patch_resp.status_code == 204:
            print(f"✅ Stock actualizado para {product_name}: {current_stock} → {new_stock}")
        else:
            print(f"❌ Error actualizando stock para {product_name}: {patch_resp.text}")
