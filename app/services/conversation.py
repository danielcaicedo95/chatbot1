from datetime import datetime, timedelta
import json

from app.utils.memory import user_histories, user_orders
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products
from app.services.orders import create_order, update_order


def find_similar_products(requested, catalog):
    requested_lower = requested.lower()
    exact = [p for p in catalog if requested_lower in p["name"].lower()]
    if exact:
        return exact

    similar = [p for p in catalog if any(w in p["name"].lower() for w in requested_lower.split())]
    return similar


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

        # Obtener catálogo
        productos = await get_all_products()
        contexto = "Catálogo actual:\n" + "\n".join(
            f"- {p['name']} ({p.get('size', 'botella estándar')}): ${p['price']}" for p in productos
        )

        # Construir prompt con instrucciones y catálogo
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
            '{"order_details": {"name": "NOMBRE", "address": "DIRECCIÓN", "phone": "TELÉFONO", '
            '"products": [{"name": "NOMBRE", "quantity": CANTIDAD, "price": PRECIO}], "total": TOTAL}}\n'
            "```\n"
            "Si el usuario modifica algo dentro de los siguientes 5 minutos, reemplaza el pedido anterior.\n"
            "Responde como un amigo, sin tecnicismos. 😄"
        )

        user_histories[from_number][-1]["text"] = instrucciones
        respuesta = await ask_gemini_with_history(user_histories[from_number])

        # Intentar extraer bloque JSON
        order_data = None
        respuesta_limpia = respuesta
        try:
            json_start = respuesta.rfind('{"order_details":')
            if json_start != -1:
                json_end = respuesta.rfind('}') + 1
                json_text = respuesta[json_start:json_end]
                parsed = json.loads(json_text)
                order_data = parsed.get("order_details")
                respuesta_limpia = respuesta[:json_start].strip()
                print(f"✅ Pedido detectado: {order_data}")
            else:
                print("ℹ️ No se encontró JSON.")
        except Exception as e:
            print(f"⚠️ Error extrayendo JSON: {e}")

        # Guardar y enviar respuesta limpia
        user_histories[from_number].append({
            "role": "model",
            "text": respuesta_limpia,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", respuesta_limpia)
        send_whatsapp_message(from_number, respuesta_limpia)

        # Crear o actualizar pedido en Supabase
        if order_data and order_data.get("products"):
            now = datetime.utcnow()
            previous = user_orders.get(from_number)

            order_data["phone"] = order_data.get("phone", from_number)
            if previous and (now - previous["timestamp"]) <= timedelta(minutes=5):
                await update_order(previous["id"], order_data)
                print(f"♻️ Pedido actualizado para {from_number}")
            else:
                new_order = await create_order(
                    phone=order_data["phone"],
                    name=order_data.get("name", ""),
                    address=order_data.get("address", ""),
                    products=order_data.get("products", []),
                    total=float(order_data.get("total", 0.0))
                )
                user_orders[from_number] = {
                    "id": new_order["id"],
                    "timestamp": now
                }
                print(f"🛒 Pedido creado para {from_number}")

    except Exception as e:
        print(f"❌ Error procesando mensaje: {e}")
