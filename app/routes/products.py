from fastapi import APIRouter, HTTPException
from app.services.products import get_all_products, get_product_by_id, create_product

router = APIRouter(prefix="/products")

@router.get("/")
async def list_products():
    return await get_all_products()

@router.get("/{product_id}")
async def get_product(product_id: str):
    product = await get_product_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product[0]

@router.post("/")
async def add_product(product: dict):
    result = await create_product(product)
    return result
