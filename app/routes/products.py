from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from typing import List, Optional, Union
import json
import io
import csv

from app.services.products import (
    create_product,
    get_all_products,
    get_product_by_id,
    create_variant,
    create_variant_image,
    delete_product
)
from app.services.supabase import upload_image_to_supabase_storage
from fastapi import File, UploadFile

router = APIRouter(prefix="/products", tags=["products"])

# --- Existing CRUD Endpoints ---
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
    variants: List[str] = Form(default=[]),  # JSON strings
    images: List[UploadFile] = File(default=[]),
):
    # Validation
    if price < 0 or stock < 0:
        raise HTTPException(status_code=400, detail="`price` and `stock` must be non-negative")

    # Upload images
    image_urls: List[str] = []
    for img in images:
        data = await img.read()
        ok, url_or_err = await upload_image_to_supabase_storage(data, img.filename, img.content_type)
        if not ok:
            raise HTTPException(status_code=500, detail=f"Image upload failed: {url_or_err}")
        image_urls.append(url_or_err)

    # Create product
    new_product = {"name": name, "description": description, "price": price, "stock": stock}
    try:
        created = await create_product(new_product)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating product: {e}")
    prod_id = created.get("id")

    # Base images
    for url in image_urls:
        await create_variant_image({
            "product_id": prod_id,
            "variant_id": None,
            "variant_label": None,
            "url": url,
        })

    # Variants
    for idx, variant_str in enumerate(variants):
        try:
            options = json.loads(variant_str)
        except:
            options = {"option": variant_str}
        variant_label = ",".join(f"{k}:{v}" for k, v in options.items())
        var = await create_variant({"product_id": prod_id, "options": options, "price": price, "stock": stock})
        var_id = var.get("id")
        if idx < len(image_urls):
            await create_variant_image({
                "product_id": prod_id,
                "variant_id": var_id,
                "variant_label": variant_label,
                "url": image_urls[idx],
            })
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

# --- New Bulk Import Endpoint ---
@router.post("/bulk", summary="Import products in bulk from CSV or JSON")
async def bulk_import(
    file: UploadFile = File(...),
    format: str = Form(...),  # 'csv' or 'json'
):
    """
    Espera un archivo CSV con columnas: name,description,price,stock,variants (JSON),image_urls (JSON array)
    O un JSON array con objetos similares.
    """
    content = await file.read()
    items = []
    try:
        if format == 'csv':
            text = content.decode('utf-8')
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                items.append(row)
        elif format == 'json':
            items = json.loads(content)
        else:
            raise HTTPException(status_code=400, detail="Format must be 'csv' or 'json'")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error parsing file: {e}")

    results = []
    for item in items:
        # Parse fields
        name = item.get('name')
        description = item.get('description')
        price = float(item.get('price', 0))
        stock = int(item.get('stock', 0))
        variants = item.get('variants') or '[]'
        image_urls = json.loads(item.get('image_urls', '[]'))

        # Create product
        prod = await create_product({"name": name, "description": description, "price": price, "stock": stock})
        pid = prod.get('id')
        # Base images
        for url in image_urls:
            await create_variant_image({"product_id": pid, "variant_id": None, "variant_label": None, "url": url})
        # Variants
        for vstr in json.loads(variants):
            options = vstr if isinstance(vstr, dict) else {"option": vstr}
            label = ",".join(f"{k}:{v}" for k,v in options.items())
            var = await create_variant({"product_id": pid, "options": options, "price": price, "stock": stock})
            vid = var.get('id')
            # no additional images by default
        results.append({"product_id": pid, "name": name})

    return {"imported": results}
