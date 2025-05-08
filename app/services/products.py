# app/services/products.py
import os
import httpx
from app.core.config import SUPABASE_URL, SUPABASE_KEY

# Cabeceras comunes para llamadas a Supabase REST
headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    # Preferimos que devuelva el nuevo registro tras POST/PATCH
    "Prefer": "return=representation"
}

async def get_all_products():
    """
    Obtiene todos los productos, incluyendo sus variantes e imágenes.
    """
    url = (
        f"{SUPABASE_URL}/rest/v1/products"
        "?select=*,product_variants(*),product_images(*)"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

async def get_product_by_id(product_id: str):
    """
    Obtiene un solo producto (por su id), con variantes e imágenes.
    """
    url = (
        f"{SUPABASE_URL}/rest/v1/products"
        f"?id=eq.{product_id}&select=*,product_variants(*),product_images(*)"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

async def create_product(data: dict):
    """
    Inserta un producto en la tabla `products`.
    Se espera que `data` incluya al menos:
      - name        (string)
      - description (string o null)
      - price       (numeric, no negativo)
      - stock       (integer, no negativo)
    Devuelve el registro creado.
    """
    url = f"{SUPABASE_URL}/rest/v1/products"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=data)
        resp.raise_for_status()
        # Supabase devuelve lista de registros (aun cuando es uno)
        created = resp.json()
        return created[0] if isinstance(created, list) and created else None

async def create_variant(variant: dict):
    """
    Inserta una variante en la tabla `product_variants`.
    Variant debe contener:
      - product_id (uuid)
      - options    (json/obj)
      - price      (numeric)
      - stock      (integer)
      - sku        (opcional)
    """
    url = f"{SUPABASE_URL}/rest/v1/product_variants"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=variant)
        resp.raise_for_status()
        data = resp.json()
        return data[0] if isinstance(data, list) and data else None

async def create_variant_image(image_record: dict):
    """
    Inserta un registro en `product_images`.
    image_record debe incluir:
      - product_id (uuid)
      - variant_id (uuid o null)
      - url        (string)
    """
    url = f"{SUPABASE_URL}/rest/v1/product_images"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=image_record)
        resp.raise_for_status()
        data = resp.json()
        return data[0] if isinstance(data, list) and data else None

async def search_products_by_keyword(keyword: str):
    """
    Busca productos cuyo `name` contenga el keyword (case-insensitive).
    """
    url = (
        f"{SUPABASE_URL}/rest/v1/products"
        f"?name=ilike.*{keyword}*&select=*,product_variants(*),product_images(*)"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

async def update_product_stock(product_name: str, quantity_sold: int):
    """
    Resta `quantity_sold` del stock del primer producto cuyo nombre contenga `product_name`.
    """
    # 1) Buscar producto
    search_url = (
        f"{SUPABASE_URL}/rest/v1/products"
        f"?name=ilike.*{product_name.replace(' ', '%20')}*&select=id,stock,name"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(search_url, headers=headers)
        resp.raise_for_status()
        items = resp.json()
        if not items:
            print(f"❌ Producto '{product_name}' no encontrado.")
            return
        product = items[0]
        new_stock = max(0, product["stock"] - quantity_sold)

        # 2) Actualizar stock
        patch_url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product['id']}"
        patch_data = {"stock": new_stock}
        patch_resp = await client.patch(patch_url, headers=headers, json=patch_data)
        patch_resp.raise_for_status()
        print(f"✅ Stock actualizado for '{product['name']}': {product['stock']} → {new_stock}")


# al final de app/services/products.py

async def get_recommended_products(pedido: list):
    """Devuelve productos recomendables según los productos del pedido."""
    all_products = await get_all_products()
    pedido_keywords = [p["name"].lower() for p in pedido]

    recomendaciones = []
    for p in all_products:
        keywords = p.get("recommended_for") or []
        if any(k in kw.lower() for kw in keywords for k in pedido_keywords):
            recomendaciones.append(p)

    nombres_pedido = [p["name"].lower() for p in pedido]
    recomendaciones_filtradas = [
        r for r in recomendaciones if r["name"].lower() not in nombres_pedido
    ]
    return recomendaciones_filtradas[:3]


# app/services/products.py (al final)

async def delete_product(product_id: str):
    """Borra un producto y devuelve True si fue exitoso."""
    url = f"{SUPABASE_URL}/rest/v1/products?id=eq.{product_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)
        # 204 No Content o 200 OK
        return resp.status_code in (200, 204)

async def delete_variant(variant_id: str):
    """Borra una variante por su ID."""
    url = f"{SUPABASE_URL}/rest/v1/product_variants?id=eq.{variant_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)
        return resp.status_code in (200, 204)
