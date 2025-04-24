from datetime import datetime
import json

from app.utils.memory import user_histories, user_orders
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products
from app.services.orders import create_order


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

        # 1. Guardar mensaje del usuario
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", text)

        # 2. Primer contacto: mensaje de bienvenida
        if len(user_histories[from_number]) == 1:
            greeting = (
                "¡Hola! 👋 Soy el asistente de *Licores El Roble*. "
                "¿Te gustaría ver nuestros productos, resolver una duda o hacer un pedido? 🍷"
            )
            user_histories[from_number].append({
                "role": "model",
                "text": greeting,
                "time": datetime.utcnow().isoformat()
            })
            await save_message_to_supabase(from_number, "model", greeting)
            send_whatsapp_message(from_number, greeting)
            return

        # 3. Obtener catálogo actual
        productos = await get_all_products()
        contexto = "Catálogo actual:\n"
        for p in productos:
            size = p.get("size", "botella estándar")
            contexto += f"- {p['name']} ({size}): ${p['price']}\n"

        # 4. Armar prompt profesional para Gemini
        user_message = (
            f"{text}\n\n"
            f"{contexto}\n"
            "Instrucciones de venta:\n"
            "1. Engancha con un mensaje amistoso.\n"
            "2. Pregunta si quieren ver productos, resolver dudas o comprar.\n"
            "3. Muestra productos populares o promos.\n"
            "4. Añade urgencia o prueba social.\n"
            "5. Llama a la acción suave: ¿reservamos uno?\n"
            "6. Si aceptan, pide en orden: nombre, teléfono, dirección, pago.\n"
            "7. Confirma pedido y ofrece seguimiento.\n\n"
            "*** IMPORTANTE: Cuando llegues al paso 7 y confirmes el pedido: ***\n"
            "DEBES incluir al FINAL de TODA tu respuesta un bloque JSON con los detalles del pedido recopilado. "
            "Estructura:\n"
            "```json\n"
            '{"order_details": {"name": "NOMBRE_CLIENTE", "address": "DIRECCION_ENTREGA", "phone": "TELEFONO_CONTACTO", '
            '"products": [{"name": "NOMBRE_PRODUCTO", "quantity": CANTIDAD, "price": PRECIO_UNITARIO}], "total": TOTAL_NUMERO}}\n'
            "```\n"
            "Si falta algún dato, usa \"\" o null. El total debe ser un número sin símbolo $.\n"
            "Responde como humano, sin tecnicismos ni IDs. Usa emojis y tono cálido."
        )

        user_histories[from_number][-1] = {
            "role": "user",
            "text": user_message,
            "time": datetime.utcnow().isoformat()
        }

        # 5. Obtener respuesta de Gemini
        history = user_histories[from_number]
        respuesta_completa = await ask_gemini_with_history(history)

        # 5b. Intentar extraer el bloque JSON
        order_data = None
        respuesta_limpia = respuesta_completa
        try:
            json_start = respuesta_completa.rfind('{"order_details":')
            if json_start != -1:
                json_end = respuesta_completa.rfind('}') + 1
                json_text = respuesta_completa[json_start:json_end]
                parsed = json.loads(json_text)
                order_data = parsed.get("order_details")
                respuesta_limpia = respuesta_completa[:json_start].strip()
                print(f"✅ Pedido extraído: {order_data}")
            else:
                print("ℹ️ No se encontró bloque JSON.")
        except Exception as e:
            print(f"⚠️ Error al parsear JSON: {e}")

        # 6. Guardar y enviar solo el mensaje limpio
        user_histories[from_number].append({
            "role": "model",
            "text": respuesta_limpia,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", respuesta_limpia)
        send_whatsapp_message(from_number, respuesta_limpia)

        # 7. Crear el pedido si hay datos válidos
        if order_data and order_data.get("products"):
            try:
                await create_order(
                    phone=order_data.get("phone", from_number),
                    name=order_data.get("name", ""),
                    address=order_data.get("address", ""),
                    products=order_data.get("products", []),
                    total=float(order_data.get("total", 0.0))
                )
                print(f"🛒 Pedido creado exitosamente para {from_number}")
            except Exception as e:
                print(f"❌ Error creando pedido: {e}")

    except Exception as e:
        print(f"❌ Error procesando el mensaje: {e}")
