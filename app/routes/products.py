from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import List
from app.services.products import create_product, get_all_products, get_product_by_id
from app.services.supabase import upload_image_to_supabase_storage

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
async def add_product(
    name: str = Form(...),
    description: str = Form(...),
    price: int = Form(...),
    stock: int = Form(...),
    images: List[UploadFile] = File(default=[])
):
    image_urls = []

    # Subir imágenes a Supabase Storage
    for image in images:
        file_data = await image.read()
        success, result = await upload_image_to_supabase_storage(file_data, image.filename, image.content_type)
        if not success:
            raise HTTPException(status_code=500, detail=f"Error subiendo imagen: {result}")
        image_urls.append(result)

    # Construir el producto con la primera imagen principal y todas en `images`
    new_product = {
        "name": name,
        "description": description,
        "price": price,
        "stock": stock,
        "image_url": image_urls[0] if image_urls else None,
        "images": image_urls  # asegúrate que tu tabla `products` tenga un campo tipo `jsonb`
    }

    result = await create_product(new_product)
    return result
