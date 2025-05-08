from datetime import datetime
import json
import re
import traceback
from difflib import get_close_matches

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order

# Campos obligatorios para confirmar pedido
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        # 1) Extraer y validar mensaje
        print("🔍 [DEBUG] Incoming webhook payload:\n", json.dumps(body, indent=2, ensure_ascii=False))
        changes = body.get("entry", [{}])[0].get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages")
        if not messages:
            return  # nada que procesar

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        if not raw_text or not from_number:
            return
        print(f"🔍 [DEBUG] From: {from_number}, Text: '{raw_text}'")

        # 2) Almacenar historia y mensaje
        user_histories.setdefault(from_number, []).append({
            "role": "user", "text": raw_text, "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # 3) Cargar productos y recomendar
        productos = await get_all_products()

        # ─── 5) BLOQUE MULTIMEDIA ─────────────────────────────────────────────
        # Solo si el usuario pide foto explícitamente
        if re.search(r"\bfoto\b|imagen|foto(s)? de", raw_text, re.I):
            # Construir catálogo enriquecido
            catalog = []
            for p in productos:
                variants = []
                for v in p.get("product_variants", []):
                    opts = v.get("options", {})
                    if not opts:
                        continue
                    value = next(iter(opts.values())).lower()
                    key0 = next(iter(opts.keys()))
                    label = v.get("variant_label") or f"{key0}:{value}"
                    imgs = [img["url"] for img in p.get("product_images", [])
                            if img.get("variant_id") == v.get("id")]
                    variants.append({"id": v["id"], "value": value, "label": label, "images": imgs})
                main_imgs = [img["url"] for img in p.get("product_images", [])
                             if img.get("variant_id") is None]
                catalog.append({"name": p["name"], "variants": variants, "images": main_imgs})

            prompt = {
                "user_request": raw_text,
                "catalog": catalog,
                "instructions": [
                    "Devuelve JSON EXACTO sin markdown:",
                    "{'want_images': true, 'target': 'valor variante o nombre producto'}",
                    "o si no quiere imágenes: {'want_images': false'}"
                ]
            }
            hist = [m for m in user_histories[from_number] if m["role"] in ("user","model")]
            resp = await ask_gemini_with_history(hist[-10:]+[{"role":"user","text":json.dumps(prompt, ensure_ascii=False)}])
            print("🔍 [DEBUG] Raw multimedia response:\n", resp)
            action = {"want_images": False}
            m = re.search(r"\{[\s\S]*\}", resp)
            if m:
                try:
                    action = json.loads(m.group())
                except: pass
            print("🔍 [DEBUG] Parsed action:", action)

            if action.get("want_images"):
                target = action.get("target","").strip().lower()
                prod = var = None
                # Exact variant match
                for e in catalog:
                    for v in e["variants"]:
                        if v["value"] == target:
                            prod_obj = next(p for p in productos if p["name"]==e["name"])
                            prod, var = e, v
                            break
                    if prod: break
                # Exact product match
                if not prod:
                    for e in catalog:
                        if e["name"].lower()==target:
                            prod_obj = next(p for p in productos if p["name"]==e["name"])
                            prod, var = e, None
                            break
                # Substring / fallback
                if not prod:
                    for e in catalog:
                        for v in e["variants"]:
                            if v["value"] in target:
                                prod_obj = next(p for p in productos if p["name"]==e["name"])
                                prod, var = e, v
                                break
                        if prod: break
                if not prod:
                    choices = [v["value"] for e in catalog for v in e["variants"]] + [e["name"].lower() for e in catalog]
                    m0 = get_close_matches(target, choices, n=1, cutoff=0.5)
                    if m0:
                        key = m0[0]
                        for e in catalog:
                            for v in e["variants"]:
                                if v["value"]==key:
                                    prod_obj = next(p for p in productos if p["name"]==e["name"])
                                    prod, var = e, v
                                    break
                            if prod: break
                        if not prod:
                            for e in catalog:
                                if e["name"].lower()==key:
                                    prod_obj = next(p for p in productos if p["name"]==e["name"])
                                    prod, var = e, None
                                    break
                if not prod:
                    send_whatsapp_message(from_number, "Lo siento, no encontré imágenes para eso. ¿Algo más?")
                    return
                urls = var["images"] if var else prod["images"]
                if not urls:
                    urls = prod_obj.get("product_images", [])
                caption = var["label"] if var else prod_obj["name"]
                send_whatsapp_message(from_number, f"¡Claro! Aquí las imágenes de *{caption}* 📸")
                for u in urls:
                    send_whatsapp_image(from_number, u, caption=caption)
                return

        # 6) Flujo de productos y pedido
        # Presentar catálogo resumido amigable
        catálogo_text = "Nuestro catálogo disponible:"
        for p in productos:
            líneas = [f"{p['name']} - COP {p['price']} (stock {p['stock']})"]
            vars_ = p.get('product_variants', [])
            if vars_:
                subt = []
                for v in vars_:
                    opts = ",".join(f"{k}:{v2}" for k,v2 in v['options'].items())
                    subt.append(f"{opts} (stk {v['stock']})")
                líneas.append("Variantes: " + "; ".join(subt))
            catálogo_text += "\n" + " - ".join(líneas)
        send_whatsapp_message(from_number, catálogo_text)

        # 7) Procesar pedido vía LLM
        contexto = catálogo_text
        instrucciones = (
            f"Usuario: {raw_text}\n{contexto}\n"
            "Responde con tono cercano e incluye JSON al confirmar pedido."
        )
        hist2 = [m for m in user_histories[from_number] if m['role'] in ('user','model')]
        resp2 = await ask_gemini_with_history(hist2 + [{'role':'user','text':instrucciones}])
        # Extraer pedido y text
        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(resp2)
        user_histories[from_number].append({'role':'model','text':clean_text,'time':datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, 'model', clean_text)
        send_whatsapp_message(from_number, clean_text)

        # 8) Finalizar con recomendaciones o procesar orden
        if order_data and order_data.get('products'):
            recs = await get_recommended_products(order_data['products'])
            if recs:
                texto = "Te recomiendo también:\n" + "\n".join(f"- {r['name']} COP {r['price']}" for r in recs)
                send_whatsapp_message(from_number, texto)

            result = await process_order(from_number, order_data)
            if result.get('status')=='missing':
                falt = result.get('fields', [])
                send_whatsapp_message(from_number, "Faltan datos:\n" + "\n".join(falt))
            elif result.get('status') in ('created','updated'):
                send_whatsapp_message(from_number, "Gracias por tu pedido! 🎉")
            else:
                send_whatsapp_message(from_number, "Ocurrió un error procesando tu pedido.")
    except Exception:
        print("❌ [ERROR] in handle_user_message:\n", traceback.format_exc())
