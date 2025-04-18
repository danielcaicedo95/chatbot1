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
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        return resp.json()
