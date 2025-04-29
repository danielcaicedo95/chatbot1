from datetime import datetime, timedelta
import json

from app.utils.memory import user_histories, user_orders, user_pending_data
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, update_product_stock, get_recommended_products
from app.services.orders import create_order, update_order

# Campos obligatorios para confirmar pedido
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

def find_similar_products(requested, catalog):
    requested_lower = requested.lower()
    exact = [p for p in catalog if requested_lower in p["name"].lower()]
    if exact:
        return exact
    return [p for p in catalog if any(w in p["name"].lower() for w in requested_lower.split())]

def extract_order_data(text: str):
    """Extrae el bloque JSON de pedido y devuelve (order_data_dict, texto_sin_json)."""
    try:
        idx = text.rfind('{"order_details":')
        if idx != -1:
            end = text.rfind('}') + 1
            js = text[idx:end]
            parsed = json.loads(js)
            return parsed.get("order_details"), text[:idx].strip()
    except Exception as e:
        print("⚠️ Error extrayendo JSON:", e)
    return None, text

def get_missing_fields(data: dict):
    """Devuelve la lista de campos REQUIRED_FIELDS que estén vacíos o nulos."""
    missing = []
    for f in REQUIRED_FIELDS:
        val = data.get(f)
        if not val or (isinstance(val, str) and val.strip().lower().startswith("tu ")):
            missing.append(f)
    return missing

async def handle_user_message(body: dict):
    try:
        entry = body['entry'][0]
        changes = entry['changes'][0]
        messages = changes['value'].get('messages')
        if not messages:
            return

        msg = messages[0]
        text = msg.get('text', {}).get('body', '').strip()
        from_number = msg.get('from')
        if not text or not from_number:
            return

        # 1) Guardar mensaje en historial y Supabase
        user_histories.setdefault(from_number, []).append({
            "role": "user", "text": text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", text)

        # 2) Primer saludo
        if len(user_histories[from_number]) == 1:
            saludo = (
                "¡Hola! 👋 Soy el asistente de *Licores El Roble*. "
                "¿Quieres ver nuestro catálogo, resolver alguna duda o hacer un pedido? 🍻"
            )
            user_histories[from_number].append({
                "role": "model", "text": saludo,
                "time": datetime.utcnow().isoformat()
            })
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        # 3) Obtener catálogo y armar prompt
        productos = await get_all_products()
        contexto = "Catálogo actual:\n" + "\n".join(
            f"- {p['name']} ({p.get('size','botella estándar')}): ${p['price']}"
            for p in productos
        )

        instrucciones = (
            f"{text}\n\n"
            f"{contexto}\n"
            "INSTRUCCIONES para el asistente:\n"
            "1. Si algún producto no está disponible, sugiere una alternativa similar.\n"
            "2. Al detectar intención de compra, responde con:\n"
            "   - Lista de productos con cantidades y precios\n"
            "   - Subtotal + $5000 de envío\n"
            "   - ¿Deseas algo más?\n"
            "   - Recomienda 1 producto adicional para acompañar (guayabo, snacks, etc.)\n"
            "   - Si dice “no”, pide datos (nombre, dirección, teléfono, pago).\n"
            "3. Incluye emojis y tono humano.\n"
            "4. Al confirmar, añade al final este JSON:\n"
            "```json\n"
            '{"order_details":{"name":"NOMBRE","address":"DIRECCIÓN","phone":"TELÉFONO",'
            '"payment_method":"TIPO_PAGO","products":[{"name":"NOMBRE","quantity":1,"price":0}],'
            '"total":0}}\n'
            "```\n"
            "Si modifica dentro de 5 min, actualiza el pedido.\n"
            "Responde como un amigo. 😄"
        )

        # Reemplazamos el último mensaje
        user_histories[from_number][-1]["text"] = instrucciones
        gemini_resp = await ask_gemini_with_history(user_histories[from_number])

        # 4) Extraer JSON y limpiar texto
        order_data, clean_text = extract_order_data(gemini_resp)

        # Guardar respuesta limpia
        user_histories[from_number].append({
            "role": "model", "text": clean_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", clean_text)
        # 🔍 Buscar recomendaciones desde la base de datos según lo que pidió el usuario
        if order_data:
            # Fusionar campos dados por el usuario explícitamente (sin autocompletar vacíos)
            pending = user_pending_data.get(from_number, {})
            for key, value in order_data.items():
                # Aceptamos solo si no es un placeholder y no es None
                if isinstance(value, str) and value.strip().lower().startswith("tu "):
                    continue
                if value:  # Solo valores explícitos
                    pending[key] = value
            user_pending_data[from_number] = pending

            # Comprobar campos faltantes
            faltantes = get_missing_fields(pending)
            if faltantes:
                texto = "Para completar tu pedido necesito:\n" + "\n".join(
                    f"- {f.replace('_',' ')}" for f in faltantes
                )
                send_whatsapp_message(from_number, f"📋 {texto}")
                return


        send_whatsapp_message(from_number, clean_text)

        # 5) Si hubo JSON de pedido, lo procesamos
        if order_data:
            # Fusionar en datos pendientes
            pending = user_pending_data.get(from_number, {})
            pending.update(order_data)
            user_pending_data[from_number] = pending

            # Marcar placeholders como vacíos
            for f in REQUIRED_FIELDS:
                v = pending.get(f, "")
                if isinstance(v, str) and v.strip().lower().startswith("tu "):
                    pending[f] = None

            # Comprobar campos faltantes
            faltantes = get_missing_fields(pending)
            if faltantes:
                texto = "Para completar tu pedido necesito:\n" + "\n".join(
                    f"- {f.replace('_',' ')}" for f in faltantes
                )
                send_whatsapp_message(from_number, f"📋 {texto}")
                return

            # 6) Todos los datos están; creamos o actualizamos
            now = datetime.utcnow()
            prev = user_orders.get(from_number)
            # Llamamos siempre con todos los parámetros nombrados
            if prev and (now - prev["timestamp"]) <= timedelta(minutes=5):
                updated = await update_order(
                    phone=pending["phone"],
                    name=pending["name"],
                    address=pending["address"],
                    products=pending["products"],
                    total=float(pending["total"]),
                    payment_method=pending["payment_method"]
                )
                if updated and updated.get("id"):
                    # 🔄 También restamos stock cuando el pedido se actualiza
                    for prod in pending["products"]:
                        await update_product_stock(prod["name"], prod["quantity"])
                    send_whatsapp_message(from_number, "♻️ Pedido actualizado y stock descontado correctamente.")
                    # Limpiar datos pendientes
                    user_pending_data.pop(from_number, None)
                else:
                    send_whatsapp_message(from_number, "❌ No pude actualizar tu pedido. Intenta de nuevo.")
            else:
                new = await create_order(
                    phone=pending["phone"],
                    name=pending["name"],
                    address=pending["address"],
                    products=pending["products"],
                    total=float(pending["total"]),
                    payment_method=pending["payment_method"]
                )
                if new and new.get("id"):
                    user_orders[from_number] = {"id": new["id"], "timestamp": now}
                    # 🔄 Restar inventario por cada producto comprado
                    for prod in pending["products"]:
                        await update_product_stock(prod["name"], prod["quantity"])
                    send_whatsapp_message(from_number, "✅ ¡Tu pedido ha sido confirmado! Gracias 🥳")
                    # Limpiar datos pendientes
                    user_pending_data.pop(from_number, None)
                else:
                    send_whatsapp_message(from_number, "❌ Lo siento, no pude guardar tu pedido. Intenta de nuevo.")


    except Exception as e:
        print("❌ Error procesando mensaje:", e)
