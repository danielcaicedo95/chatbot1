# app/services/conversation.py

from datetime import datetime, timedelta, timezone
import json
import re

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import (
    get_all_products,
    get_recommended_products,
)
from app.services.orders import process_order

# Campos obligatorios para confirmar pedido
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        messages = changes["value"].get("messages")
        if not messages:
            return

        msg = messages[0]
        text = msg.get("text", {}).get("body", "").strip().lower()
        from_number = msg.get("from")
        if not text or not from_number:
            return

        # 1) Guardar mensaje en historial y Supabase
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", text)

        # 2) Primer saludo
        if len(user_histories[from_number]) == 1:
            saludo = (
                "¡Hola! 👋 Soy el asistente de *Licores El Roble*.\n"
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

        # 3) Si pide fotos, enviamos imágenes del producto nombrado
        if re.search(r"\bfoto(s)?\b|\bimagen(es)?\b", text):
            productos = await get_all_products()
            # Intentar extraer nombre de producto de su mensaje
            sent_any = False
            for p in productos:
                if p["name"].lower() in text:
                    # Enviar imágenes generales
                    gen_imgs = [img["url"] for img in p.get("product_images", []) if img["variant_id"] is None]
                    # Enviar imágenes de variantes
                    var_imgs = [img["url"] for img in p.get("product_images", []) if img["variant_id"]]
                    for url in gen_imgs + var_imgs:
                        send_whatsapp_message(from_number, url)  # asumiendo que envía media cuando es URL de imagen
                        sent_any = True
            if not sent_any:
                send_whatsapp_message(from_number, "Lo siento, no encontré imágenes de ese producto.")
            return

        # 4) Obtener catálogo y armar prompt con variantes e imágenes
        productos = await get_all_products()
        contexto_lines = []
        for p in productos:
            line = f"- {p['name']}: COP {p['price']} (stock {p['stock']})"
            # variantes
            vars = p.get("product_variants") or []
            if vars:
                opts = ", ".join(
                    f"{list(v['options'].values())[0]} (stock {v['stock']})"
                    for v in vars
                )
                line += f" | Variantes: {opts}"
            # cuenta imágenes
            imgs = p.get("product_images") or []
            if imgs:
                line += f" | Imágenes disponibles: {len(imgs)}"
            contexto_lines.append(line)
        contexto = "Catálogo actual:\n" + "\n".join(contexto_lines)

        instrucciones = (
            f"{text}\n\n"
            f"{contexto}\n\n"
            "INSTRUCCIONES para el asistente:\n"
            "1. Si algún producto no está disponible, sugiere una alternativa.\n"
            "2. Al detectar intención de compra, despliega:\n"
            "   - Lista de productos con cantidades y precios\n"
            "   - Subtotal + COP 5.000 de envío\n"
            "   - ¿Deseas algo más?\n"
            "   - Recomienda 1 producto adicional.\n"
            "   - Si dice “no”, pide datos (nombre, dirección, teléfono, pago).\n"
            "3. Incluye emojis y tono cercano.\n"
            "4. Al confirmar, añade al final este JSON exacto:\n"
            "{\"order_details\":{\"name\":\"NOMBRE\",\"address\":\"DIRECCIÓN\",\"phone\":\"TELÉFONO\",\"payment_method\":\"TIPO_PAGO\",\"products\":[{\"name\":\"NOMBRE\",\"quantity\":1,\"price\":0}],\"total\":0}}\n"
            "Si modifica dentro de 5 min, actualiza el pedido.\n"
            "Responde como un amigo. 😄"
        )

        # Reemplazar el último mensaje en historial con instrucciones
        user_histories[from_number][-1]["text"] = instrucciones
        gemini_resp = await ask_gemini_with_history(user_histories[from_number])

        # 5) Extraer JSON de pedido y mensaje limpio
        from app.utils.extractors import extract_order_data
        order_data, clean_text = extract_order_data(gemini_resp)

        # Guardar respuesta limpia
        user_histories[from_number].append({
            "role": "model",
            "text": clean_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "model", clean_text)

        # 6) Agregar recomendaciones si aplica
        if order_data and order_data.get("products"):
            recomendaciones = await get_recommended_products(order_data["products"])
            if recomendaciones:
                texto_rec = "\n".join(
                    f"- {r['name']}: COP {r['price']}"
                    for r in recomendaciones
                )
                rec_msg = (
                    "\n🧠 Basado en tu pedido, podrías acompañarlo con:\n"
                    f"{texto_rec}\n¿Te gustaría agregar alguno?"
                )
                clean_text += rec_msg
                send_whatsapp_message(from_number, rec_msg)
            else:
                send_whatsapp_message(from_number, clean_text)
        else:
            send_whatsapp_message(from_number, clean_text)

        # 7) Procesar pedido si se extrajo JSON
        if order_data:
            result = await process_order(from_number, order_data)
            status = result.get("status")
            if status == "missing":
                campos = "\n".join(f"- {f.replace('_',' ')}" for f in result.get("fields", []))
                send_whatsapp_message(from_number, f"📋 Para completar tu pedido necesito:\n{campos}")
            elif status == "created":
                send_whatsapp_message(from_number, "✅ ¡Tu pedido ha sido confirmado! Gracias 🥳")
            elif status == "updated":
                send_whatsapp_message(from_number, "♻️ Pedido actualizado correctamente.")
            else:
                send_whatsapp_message(from_number, "❌ Ocurrió un error guardando tu pedido.")

    except Exception as e:
        print("❌ Error procesando mensaje:", e)
