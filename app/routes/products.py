from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import List, Optional
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
    variants: List[str] = Form(default=[]),  # JSON strings: {"options": {...}, "price":x, "stock":y}
    images: List[UploadFile] = File(default=[]),
):
    # Validate main stock/price
    if price < 0 or stock < 0:
        raise HTTPException(status_code=400, detail="`price` and `stock` must be non-negative")

    # Upload images -> URLs
    image_urls: List[str] = []
    for img in images:
        data = await img.read()
        ok, url_or_err = await upload_image_to_supabase_storage(data, img.filename, img.content_type)
        if not ok:
            raise HTTPException(status_code=500, detail=f"Image upload failed: {url_or_err}")
        image_urls.append(url_or_err)

    # Create product record
    new_product = {"name": name, "description": description, "price": price, "stock": stock}
    try:
        created = await create_product(new_product)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating product: {e}")
    prod_id = created.get("id")

    # Save general images
    for url in image_urls:
        await create_variant_image({
            "product_id": prod_id,
            "variant_id": None,
            "variant_label": None,
            "url": url,
        })

    # Process variants (allow per-variant price/stock)
    for idx, variant_str in enumerate(variants):
        try:
            parsed = json.loads(variant_str)
        except json.JSONDecodeError:
            # legacy: single option value
            parsed = {"options": {"option": variant_str}}

        # Determine options and overrides
        if isinstance(parsed, dict) and "options" in parsed:
            options = parsed.get("options") or {}
            variant_price = float(parsed.get("price", price))
            variant_stock = int(parsed.get("stock", stock))
        else:
            options = parsed
            variant_price = price
            variant_stock = stock

        # Build human label
        variant_label = ",".join(f"{k}:{v}" for k, v in options.items())

        # Create variant record
        try:
            var = await create_variant({
                "product_id": prod_id,
                "options": options,
                "price": variant_price,
                "stock": variant_stock,
            })
        except Exception as e:
            print(f"Warning: failed to create variant: {e}")
            continue
        var_id = var.get("id")

        # Optionally attach one of the uploaded images to this variant
        if idx < len(image_urls) and var_id:
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

@router.post("/bulk", summary="Import products in bulk from CSV or JSON")
async def bulk_import(
    file: UploadFile = File(...),
    format: str = Form(...),  # 'csv' or 'json'
):
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
        # Parse global fields
        name = item.get('name')
        description = item.get('description')
        base_price = float(item.get('price', 0))
        base_stock = int(item.get('stock', 0))
        variants_json = item.get('variants') or '[]'
        image_urls = json.loads(item.get('image_urls', '[]'))

        # Create product
        prod = await create_product({
            "name": name,
            "description": description,
            "price": base_price,
            "stock": base_stock
        })
        pid = prod.get('id')

        # Base images
        for url in image_urls:
            await create_variant_image({
                "product_id": pid,
                "variant_id": None,
                "variant_label": None,
                "url": url
            })

        # Variants with overrides
        for v in json.loads(variants_json):
            if isinstance(v, dict) and 'options' in v:
                options = v.get('options') or {}
                variant_price = float(v.get('price', base_price))
                variant_stock = int(v.get('stock', base_stock))
            else:
                options = v if isinstance(v, dict) else {"option": v}
                variant_price = base_price
                variant_stock = base_stock

            variant_label = ",".join(f"{k}:{v}" for k, v in options.items())
            var = await create_variant({
                "product_id": pid,
                "options": options,
                "price": variant_price,
                "stock": variant_stock
            })
            var_id = var.get('id')
            # no default images for bulk import variants

        results.append({"product_id": pid, "name": name})

    return {"imported": results}
