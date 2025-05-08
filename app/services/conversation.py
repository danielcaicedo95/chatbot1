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
        # ─── 1) Depurar payload y extraer mensaje ───────────────────────────────
        print("🔍 [DEBUG] Incoming webhook payload:\n", json.dumps(body, indent=2, ensure_ascii=False))
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages")
        if not messages:
            print("⚠️ [DEBUG] No messages in payload")
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        print(f"🔍 [DEBUG] From: {from_number}, Text: '{raw_text}'")
        if not raw_text or not from_number:
            print("⚠️ [DEBUG] Missing text or from_number")
            return

        # ─── 2) Guardar en historial y Supabase ───────────────────────────────────
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text)

        # ─── 3) Cargar catálogo y preparar estructuras ────────────────────────────
        productos = await get_all_products()

        # Preparar choice_map si se necesitara para algún fallback interno (no expuesto al usuario)
        choice_map = {}
        def extract_labels(o, labels):
            if isinstance(o, dict):
                for v in o.values(): extract_labels(v, labels)
            elif isinstance(o, list):
                for v in o: extract_labels(v, labels)
            elif isinstance(o, str): labels.append(o)

        for p in productos:
            choice_map[p['name'].lower()] = (p, None)
            for v in p.get('product_variants', []):
                labels = []
                extract_labels(v.get('options', {}), labels)
                for label in labels:
                    choice_map[label.lower()] = (p, v)

        # Enriquecer catálogo para LLM
        catalog = []
        for p in productos:
            base_price = p.get('price', 0) or 0
            base_stock = p.get('stock', 0) if p.get('stock', 0) >= 0 else 0
            variants = []
            for v in p.get('product_variants', []):
                opts = v.get('options', {})
                v_price = v.get('price', base_price) or base_price
                v_stock = v.get('stock', base_stock) if v.get('stock', base_stock) >= 0 else base_stock
                value = next(iter(opts.values())).lower() if opts else ''
                label = v.get('variant_label') or ','.join(f"{k}:{opts[k]}" for k in opts)
                imgs = [img['url'] for img in p.get('product_images', []) if img.get('variant_id') == v.get('id')]
                variants.append({
                    'id': v.get('id'),
                    'value': value,
                    'label': label,
                    'price': v_price,
                    'stock': v_stock,
                    'images': imgs
                })
            main_imgs = [img['url'] for img in p.get('product_images', []) if img.get('variant_id') is None]
            catalog.append({
                'name': p.get('name'),
                'price': base_price,
                'stock': base_stock,
                'variants': variants,
                'images': main_imgs
            })

        # ─── 4) Construir y enviar prompt a Gemini ───────────────────────────────
        hist = user_histories[from_number][-10:]
        prompt_obj = {
            'history': hist,
            'catalog': catalog,
            'instructions': [
                "Detecta si el usuario solicita ver imágenes; devuelve {type:'images',urls:[...],caption:''} (sin texto extra al enviar),",
                "De lo contrario, responde {type:'text',content:'mensaje humano'};",
                "Para ventas, sugiere productos, calcula subtotal + COP 5.000 envío, recomienda uno más, y al final recopila datos de pedido por formularios,",
                "sin exponer ninguna estructura JSON en WhatsApp."
            ]
        }
        llm_input = hist + [{'role': 'user', 'text': json.dumps(prompt_obj, ensure_ascii=False)}]
        # Manejo robusto de fallo en Gemini
        try:
            llm_resp = await ask_gemini_with_history(llm_input)
        except Exception as e:
            print(f"❌ Error HTTP al llamar a Gemini: {e}")
            llm_resp = json.dumps({'type': 'text', 'content': 'Hubo un problema de conexión al generar la respuesta.'}, ensure_ascii=False)

        print("🔍 [DEBUG] Raw multimedia response:\n", llm_resp)

        # ─── 5) Parsear respuesta de Gemini ─────────────────────────────────────
        try:
            resp_obj = json.loads(llm_resp)
        except Exception:
            resp_obj = {'type': 'text', 'content': llm_resp}

        # ─── 6) Enviar imágenes sin texto si corresponde ────────────────────────
        if resp_obj.get('type') == 'images':
            urls = resp_obj.get('urls', [])[:10]
            for url in urls:
                await send_whatsapp_image(from_number, url)
            return

        # ─── 7) Preparar y enviar texto humano para conversación/venta ──────────
        # Garantizar mensaje válido
        user_message = resp_obj.get('content') or raw_text or 'Lo siento, algo salió mal.'
        # Guardar en historial y Supabase
        user_histories[from_number].append({'role': 'model', 'text': user_message, 'time': datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, 'model', user_message)
        await send_whatsapp_message(from_number, user_message)

        # ─── 8) Extraer datos de pedido si el usuario rellena el formulario ─────
        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(user_message)
        if not order_data or not order_data.get('products'):
            return

        # ─── 9) Recomendar productos adicionales ───────────────────────────────
        recomendaciones = await get_recommended_products(order_data['products'])
        if recomendaciones:
            texto_rec = '\n'.join(f"- {r['name']}: COP {r['price']}" for r in recomendaciones)
            await send_whatsapp_message(from_number, f"🧠 Podrías acompañar tu pedido con:\n{texto_rec}\n¿Te interesa alguno?")
            return

        # ─── 10) Procesar orden y responder según status ────────────────────────
        result = await process_order(from_number, order_data)
        status = result.get('status')
        if status == 'missing':
            campos = '\n'.join(f"- {f.replace('_', ' ')}" for f in result.get('fields', []))
            await send_whatsapp_message(from_number, f"📋 Faltan datos:\n{campos}")
        elif status == 'created':
            await send_whatsapp_message(from_number, '✅ Pedido confirmado. ¡Gracias! 🎉')
        elif status == 'updated':
            await send_whatsapp_message(from_number, '♻️ Pedido actualizado correctamente.')
        else:
            await send_whatsapp_message(from_number, '❌ Error guardando el pedido.')

    except Exception:
        print("❌ [ERROR] in handle_user_message:\n", traceback.format_exc())
