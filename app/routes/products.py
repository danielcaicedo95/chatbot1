from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import List, Optional
import json

from app.services.products import (
    create_product,
    get_all_products,
    get_product_by_id,
    create_variant,
    create_variant_image,
    delete_product  # 👈 añadido
)
from app.services.supabase import upload_image_to_supabase_storage

router = APIRouter(prefix="/products", tags=["products"])

@router.get("/", summary="List all products with variants and images")
async def list_products():
    try:
        return await get_all_products()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching products: {e}")

@router.get("/{product_id}", summary="Get a single product by ID with variants and images")
async def get_product(product_id: str):
    try:
        products = await get_product_by_id(product_id)
        if not products:
            raise HTTPException(status_code=404, detail="Product not found")
        return products[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching product: {e}")

@router.post("/", summary="Create a new product with optional variants and images")
async def add_product(
    name: str = Form(...),
    description: Optional[str] = Form(None),
    price: float = Form(...),
    stock: int = Form(...),
    variants: List[str] = Form(default=[]),
    images: List[UploadFile] = File(default=[]),
):
    if price < 0 or stock < 0:
        raise HTTPException(status_code=400, detail="`price` and `stock` must be non-negative")

    image_urls: List[str] = []
    for img in images:
        data = await img.read()
        ok, url_or_err = await upload_image_to_supabase_storage(data, img.filename, img.content_type)
        if not ok:
            raise HTTPException(status_code=500, detail=f"Image upload failed: {url_or_err}")
        image_urls.append(url_or_err)

    new_product = {
        "name": name,
        "description": description,
        "price": price,
        "stock": stock,
    }
    try:
        created = await create_product(new_product)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating product: {e}")

    prod_id = created.get("id")
    if not prod_id:
        raise HTTPException(status_code=500, detail="Product creation did not return an ID")

    for url in image_urls:
        try:
            await create_variant_image({
                "product_id": prod_id,
                "variant_id": None,
                "url": url,
            })
        except Exception as e:
            print(f"Warning: failed to save image record: {e}")

    for idx, variant_str in enumerate(variants):
        try:
            options = json.loads(variant_str)
        except json.JSONDecodeError:
            options = {"option": variant_str}

        var_payload = {
            "product_id": prod_id,
            "options": options,
            "price": price,
            "stock": stock,
        }
        try:
            variant = await create_variant(var_payload)
        except Exception as e:
            print(f"Warning: failed to create variant: {e}")
            continue

        if idx < len(image_urls):
            try:
                await create_variant_image({
                    "product_id": prod_id,
                    "variant_id": variant.get("id"),
                    "url": image_urls[idx],
                })
            except Exception as e:
                print(f"Warning: failed to save variant image: {e}")

    return created

@router.delete("/{product_id}", summary="Eliminar un producto completo")
async def remove_product(product_id: str):
    try:
        ok = await delete_product(product_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Producto no encontrado o no eliminado")
        return {"message": "Producto eliminado"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error eliminando producto: {e}")
