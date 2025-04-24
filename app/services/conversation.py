from datetime import datetime, timedelta
import json

from app.utils.memory import user_histories, user_orders, user_pending_data
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products
from app.services.orders import create_order, update_order


REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]


def find_similar_products(requested, catalog):
    requested_lower = requested.lower()
    exact = [p for p in catalog if requested_lower in p["name"].lower()]
    if exact:
        return exact
    similar = [p for p in catalog if any(w in p["name"].lower() for w in requested_lower.split())]
    return similar


def extract_order_data(text: str):
    try:
        json_start = text.rfind('{"order_details":')
        if json_start != -1:
            json_end = text.rfind('}') + 1
            json_text = text[json_start:json_end]
            parsed = json.loads(json_text)
            return parsed.get("order_details"), text[:json_start].strip()
    except Exception as e:
        print(f"⚠️ Error extrayendo JSON: {e}")
    return None, text


def get_missing_fields(order_data):
    return [field for field in REQUIRED_FIELDS if not order_data.get(field)]


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

        # Guardar mensaje del usuario
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", text)

        # Primer mensaje
        if len(user_histories[from_number]) == 1:
            saludo = (
                "¡Hola! 👋 Soy el asistente de *Licores El Roble*. "
                "¿Quieres ver nuestro catálogo, resolver alguna duda o hacer un pedido? 🍻"
            )
            user_histories[from_number].append({
                "role": "model",
                "text": saludo,
                "time": datetime.utcnow().isoformat()
            })
            await save_message_to_supabase(from_number, "model", saludo)
            send_whatsapp_message(from_number, saludo)
            return

        productos = await get_all_products()
        contexto = "Catálogo actual:\n" + "\n".join(
            f"- {p['name']} ({p.get('size', 'botella estándar')}): ${p['price']}" for p in productos
        )

        instrucciones = (
            f"{text}\n\n"
            f"{contexto}\n"
            "INSTRUCCIONES para el asistente:\n"
            "1. Si algún producto no está disponible, sugiere una alternativa similar (nombre o categoría).\n"
            "2. Al detectar intención de compra, responde con:\n"
            "- Lista de productos con cantidades y precios\n"
            "- Precio total + $5000 de envío\n"
            "- Pregunta si desea agregar algo más\n"
            "- Recomienda *1 solo producto adicional* para acompañar el pedido (para el guayabo, snacks, etc.)\n"
            "- Si el cliente dice que no desea nada más, ahí sí pide datos (nombre, dirección, pago).\n"
            "3. Siempre incluye emojis y tono humano.\n"
            "4. Al confirmar el pedido, incluye este JSON al final:\n"
            "```json\n"
            '{"order_details": {"name": "NOMBRE", "address": "DIRECCIÓN", "phone": "TELÉFONO", "payment_method": "TIPO_PAGO", '
            '"products": [{"name": "NOMBRE", "quantity": CANTIDAD, "price": PRECIO}], "total": TOTAL}}\n'
            "```\n"
            "Si el usuario modifica algo dentro de los siguientes 5 minutos, reemplaza el pedido anterior.\n"
            "Responde como un amigo, sin tecnicismos. 😄"
        )

        user_histories[from_number][-1]["text"] = instrucciones
        respuesta = await ask_gemini_with_history(user_histories[from_number])

        order_data, respuesta_limpia = extract_order_data(respuesta)

        # Guardar y enviar respuesta sin JSON
        user_histories[from_number].append({
            "role": "model",
            "text": respuesta_limpia,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", respuesta_limpia)
        send_whatsapp_message(from_number, respuesta_limpia)

        # Manejo de datos incompletos
        if order_data:
            pending = user_pending_data.get(from_number, {})
            pending.update(order_data)
            user_pending_data[from_number] = pending

            missing = get_missing_fields(pending)
            if missing:
                msg_faltantes = "Para confirmar tu pedido necesito que me digas:\n" + \
                                "\n".join(f"- Tu {f.replace('_', ' ')}" for f in missing)
                send_whatsapp_message(from_number, f"📋 {msg_faltantes}")
                return

            # Completo → guardar pedido
            now = datetime.utcnow()
            previous = user_orders.get(from_number)

            if previous and (now - previous["timestamp"]) <= timedelta(minutes=5):
                await update_order(previous["id"], pending)
                print(f"♻️ Pedido actualizado para {from_number}")
            else:
                new_order = await create_order(
                    phone=pending["phone"],
                    name=pending["name"],
                    address=pending["address"],
                    products=pending["products"],
                    total=float(pending["total"]),
                    payment_method=pending["payment_method"]
                )
                user_orders[from_number] = {"id": new_order["id"], "timestamp": now}
                print(f"🛒 Pedido creado para {from_number}")
                send_whatsapp_message(from_number, "✅ ¡Listo! Tu pedido fue confirmado. Gracias por tu compra 🥳")

    except Exception as e:
        print(f"❌ Error procesando mensaje: {e}")
