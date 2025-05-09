# app/services/conversation.py

from datetime import datetime, timezone
import json
import re
import traceback
from difflib import get_close_matches
import logging

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image
from app.services.supabase import save_message_to_supabase
# Importar funciones específicas de products.py que podríamos usar para recomendaciones más avanzadas
from app.services.products import get_all_products, get_recommended_products, search_products_by_keyword
from app.services.orders import process_order
from app.utils.extractors import extract_order_data

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

REQUIRED_CONVERSATIONAL_ORDER_FIELDS = ["name", "address", "phone", "payment_method"]

# --- Funciones de Catálogo y Matching (Mantener las de la respuesta anterior) ---
def build_structured_catalog_for_logic(productos_list: list) -> list:
    # ... (Mantener la implementación exacta de la respuesta anterior)
    structured_catalog = []
    if not productos_list: return structured_catalog
    for p_data in productos_list:
        try:
            variants_details = []
            for v_data in p_data.get("product_variants", []):
                opts = v_data.get("options", {})
                if not opts: continue
                display_label_parts = [f"{k_opt}:{v_opt_val}" for k_opt, v_opt_val in opts.items()]
                value_for_matching_parts = [str(v_opt_val).strip().lower() for v_opt_val in opts.values()]
                catalog_variant_label_parts = [f"{str(k_opt).strip().lower()}:{str(v_opt_val).strip().lower()}" for k_opt, v_opt_val in opts.items()]
                variants_details.append({
                    "id": v_data["id"], "display_label": ", ".join(display_label_parts),
                    "value_for_matching": " ".join(value_for_matching_parts),
                    "catalog_variant_label_for_images": ",".join(catalog_variant_label_parts),
                    "price": v_data.get("price"), "stock": v_data.get("stock"),
                    "images": [img["url"] for img in p_data.get("product_images", []) if img.get("variant_id") == v_data["id"]]
                })
            main_product_images = [img["url"] for img in p_data.get("product_images", []) if img.get("variant_id") is None]
            structured_catalog.append({
                "id": p_data["id"], "name": p_data["name"], "description": p_data.get("description"),
                "base_price": p_data.get("price"), "base_stock": p_data.get("stock"),
                "variants": variants_details, "main_images": main_product_images,
                "all_product_images_raw": p_data.get("product_images", [])
            })
        except Exception as e:
            logger.error(f"Error construyendo catálogo: {p_data.get('name', 'ID desc')}: {e}", exc_info=True)
    return structured_catalog

def format_catalog_for_llm_context(s_catalog: list) -> list:
    # ... (Mantener la implementación exacta de la respuesta anterior)
    llm_catalog_summary = []
    for p_entry in s_catalog:
        variants_summary = [f"{v['display_label']} (P: {v.get('price', 'N/A')}, S: {v.get('stock', 'N/A')})" for v in p_entry.get("variants", [])]
        llm_catalog_summary.append({
            "name": p_entry["name"], "description": p_entry.get("description", "N/D"),
            "price_info": f"Base COP {p_entry.get('base_price')}" if p_entry.get('base_price') and not variants_summary else "Ver variantes",
            "stock_info": f"Base: {p_entry.get('base_stock')}" if p_entry.get('base_stock') and not variants_summary else "Ver variantes",
            "variants": variants_summary if variants_summary else "Sin variantes específicas.",
            "images_available": bool(p_entry.get("main_images") or any(v.get("images") for v in p_entry.get("variants",[])) or p_entry.get("all_product_images_raw"))
        })
    return llm_catalog_summary


def match_target_in_catalog_for_images(s_catalog: list, query: str) -> tuple[dict | None, dict | None]:
    # ... (Mantener la implementación exacta de la respuesta anterior)
    if not query or not s_catalog: return None, None
    target = query.strip().lower()
    for prod in s_catalog:
        p_name_low = prod["name"].lower()
        if p_name_low in target:
            if p_name_low == target: return prod, None
            for var in prod["variants"]:
                if var["value_for_matching"] in target: return prod, var
            return prod, None
        for var in prod["variants"]:
            if var["value_for_matching"] == target: return prod, var
    choices = [p["name"].lower() for p in s_catalog] + \
              [f"{p['name'].lower()} {v['value_for_matching']}" for p in s_catalog for v in p["variants"]] + \
              [v['value_for_matching'] for p in s_catalog for v in p["variants"]]
    matches = get_close_matches(target, list(set(choices)), n=1, cutoff=0.65)
    if matches:
        match_str = matches[0]
        for prod in s_catalog:
            if prod["name"].lower() == match_str: return prod, None
            for var in prod["variants"]:
                if f"{prod['name'].lower()} {var['value_for_matching']}" == match_str: return prod, var
                if var["value_for_matching"] == match_str: return prod, var
    return None, None


# --- Lógica de Manejo de Solicitud de Imágenes ---
async def handle_image_request_logic(
    from_number: str, user_raw_text: str, current_history: list, structured_catalog_data: list
) -> tuple[bool, str | None]: # Devuelve (manejado, mensaje_error_api | None)
    try:
        # ... (Igual que antes, pero ahora devuelve también un mensaje de error si lo hay)
        catalog_summary_for_llm = [{"name": p["name"], "variants": [v["display_label"] for v in p.get("variants", [])]} for p in structured_catalog_data]
        image_intent_prompt = {
            "user_request": user_raw_text, "available_products_summary": catalog_summary_for_llm,
            "task": "Analiza 'user_request'. Si pide imágenes/fotos, responde JSON: {\"want_images\": true, \"target\": \"nombre producto/variante\"}. Si no, {\"want_images\": false}. Si el target no es claro, añade 'clarification_needed': 'mensaje'.",
            "examples": [{"user": "fotos tequila?", "bot_json": {"want_images": True, "target": "tequila"}}, {"user": "foto porfa", "bot_json": {"want_images": True, "target": "producto contexto", "clarification_needed": "¡Claro! ¿De qué producto?"}}]
        }
        llm_input = current_history[-3:] + [{"role": "user", "text": json.dumps(image_intent_prompt, ensure_ascii=False)}]
        
        logger.info(f"🧠 Gemini (Image Intent) - Enviando solicitud...")
        llm_response_text = await ask_gemini_with_history(llm_input)

        if isinstance(llm_response_text, str) and (llm_response_text.startswith("GEMINI_API_ERROR:") or llm_response_text.startswith("GEMINI_RESPONSE_ISSUE:")):
            logger.error(f"Error/Problema de Gemini (Image Intent): {llm_response_text}")
            return False, llm_response_text 

        logger.info(f"🧠 Gemini (Image Intent) - Raw Response: {llm_response_text}")
        json_match = re.search(r"\{[\s\S]*\}", llm_response_text)
        if not json_match:
            logger.warning("No JSON en respuesta LLM para intención de imagen. Asumiendo NO quiere imágenes.")
            return False, None
        
        try:
            action = json.loads(json_match.group())
        except json.JSONDecodeError:
            logger.error(f"Error decodificando JSON intención imagen. Respuesta: {json_match.group()}")
            return False, None

        if not action.get("want_images"):
            return False, None

        if action.get("clarification_needed") and isinstance(action["clarification_needed"], str):
            send_whatsapp_message(from_number, action["clarification_needed"])
            return True, None

        target_description = action.get("target")
        if not target_description or not isinstance(target_description, str):
            send_whatsapp_message(from_number, "¡Claro! Quieres fotos. ¿De qué producto o variante te gustaría verlas? 🤔")
            return True, None

        matched_product_cat_obj, matched_variant_cat_obj = match_target_in_catalog_for_images(
            structured_catalog_data, target_description
        )

        if not matched_product_cat_obj:
            send_whatsapp_message(from_number, f"Lo siento, no encontré '{target_description}' para mostrarte imágenes. 😔")
            return True, None

        image_urls_to_send = []
        display_name_for_caption = matched_product_cat_obj["name"]
        # ... (Lógica de recopilar URLs igual que antes)
        if matched_variant_cat_obj:
            display_name_for_caption = f"{matched_product_cat_obj['name']} ({matched_variant_cat_obj['display_label']})"
            image_urls_to_send.extend(matched_variant_cat_obj.get("images", []))
            if not image_urls_to_send:
                variant_id_to_match = matched_variant_cat_obj["id"]
                variant_label_to_match_img = matched_variant_cat_obj["catalog_variant_label_for_images"].lower()
                for img_obj in matched_product_cat_obj.get("all_product_images_raw", []):
                    if img_obj.get("variant_id") == variant_id_to_match or \
                       (img_obj.get("variant_label") and img_obj.get("variant_label").lower() == variant_label_to_match_img):
                        if img_obj["url"] not in image_urls_to_send: image_urls_to_send.append(img_obj["url"])
        if not image_urls_to_send:
            image_urls_to_send.extend(matched_product_cat_obj.get("main_images", []))
            image_urls_to_send = list(set(image_urls_to_send))


        if not image_urls_to_send:
            send_whatsapp_message(from_number, f"No tenemos imágenes para *{display_name_for_caption}* en este momento. ¿Te ayudo con algo más?")
            return True, None

        send_whatsapp_message(from_number, f"¡Claro! Aquí tienes las imágenes de *{display_name_for_caption}*:")
        for img_url in image_urls_to_send:
            try:
                send_whatsapp_image(from_number, img_url, caption=display_name_for_caption)
            except Exception as e_img:
                logger.error(f"❌ Error enviando imagen {img_url}: {e_img}", exc_info=True)
        return True, None # Manejado exitosamente
    except Exception as e_img_handler:
        logger.error(f"⚠️ Error en handle_image_request_logic: {e_img_handler}", exc_info=True)
        return False, f"GEMINI_API_ERROR: Error interno procesando solicitud de imagen ({type(e_img_handler).__name__})." # Devolver error si falla


# --- Flujo Principal de Mensajes ---
async def handle_user_message(body: dict):
    gemini_api_error_occurred_message = None # Para rastrear si hubo error de API en algún punto
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value_data = changes.get("value", {})
        messages = value_data.get("messages")

        if not messages:
            if value_data.get("statuses"): logger.info(f"Status update: {value_data['statuses']}")
            else: logger.info("Webhook sin 'messages'. Ignorando.")
            return

        msg = messages[0]
        user_raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        
        msg_timestamp_unix = msg.get("timestamp")
        message_time = datetime.fromtimestamp(int(msg_timestamp_unix), tz=timezone.utc) if msg_timestamp_unix else datetime.now(timezone.utc)

        if not user_raw_text or not from_number:
            logger.warning("Mensaje sin texto o remitente. Ignorando.")
            return

        logger.info(f"Mensaje de {from_number} ({message_time.isoformat()}): '{user_raw_text}'")

        current_user_history = user_histories.setdefault(from_number, [])
        current_user_history.append({
            "role": "user",
            "text": user_raw_text,
            "time": message_time.isoformat()
        })
        await save_message_to_supabase(from_number, "user", user_raw_text)

        productos_db_data = await get_all_products()
        if not productos_db_data:
            logger.error("CRÍTICO: No se pudieron obtener productos.")
            send_whatsapp_message(from_number, "Lo siento, tenemos problemas para acceder al catálogo ahora. 🛠️ Intenta más tarde.")
            return
        
        s_catalog_logic = build_structured_catalog_for_logic(productos_db_data)

        # 1. Intentar manejar como solicitud de imagen
        # handle_image_request_logic ahora devuelve (manejado, mensaje_error_api | None)
        image_request_was_handled, img_api_error_msg = await handle_image_request_logic(
            from_number, user_raw_text, current_user_history, s_catalog_logic
        )

        if img_api_error_msg: # Si hubo un error de API al intentar procesar imágenes
            gemini_api_error_occurred_message = img_api_error_msg # Guardar para posible mensaje final
            # No necesariamente retornamos, el flujo general podría aún funcionar o dar un error más genérico.

        if image_request_was_handled: # Si la lógica de imagen se completó (envió o dijo que no hay)
            logger.info("Solicitud de imagen manejada por handle_image_request_logic. Finalizando.")
            return

        # 2. Si no fue una solicitud de imagen manejada, o si falló la detección pero no críticamente,
        #    procesar como conversación general/pedido
        logger.info(f"Procesando como mensaje general/pedido para {from_number}")

        catalog_for_llm_prompt = format_catalog_for_llm_context(s_catalog_logic)
        
        # ---- INSTRUCCIONES PARA EL LLM (REFINADAS PARA CONTROL DE FLUJO) ----
        order_taking_instructions = (
            f"Eres 'VendiBot', vendedor experto de 'Licores El Roble'. Amigable, colombiano, conversacional, emojis. Tu meta es ayudar y concretar ventas.\n\n"
            f"CLIENTE DICE: \"{user_raw_text}\"\n\n"
            f"CATÁLOGO (Precios COP, stock indicado. Si agotado, informa y sugiere alternativa del catálogo):\n{json.dumps(catalog_for_llm_prompt, indent=2, ensure_ascii=False)}\n\n"
            f"**== TU PROCESO DE VENTA ==**\n"
            f"**ESTADO ACTUAL (Determina en qué estado estás basado en el historial y el último mensaje del cliente. Si no hay un pedido en curso, estás en 'Consultando'):**\n"
            f"   - 'Consultando': El cliente está preguntando, explorando. Ayúdalo, da información.\n"
            f"   - 'ArmandoCarrito': El cliente ha expresado intención de comprar al menos un producto. Ya has confirmado ítems.\n"
            f"   - 'PidiendoDatos': El cliente dijo que NO quiere añadir más productos y está listo para dar sus datos.\n"
            f"   - 'ConfirmandoPedidoFinal': Ya tienes todos los datos del cliente y el carrito, y le estás presentando el resumen final para su 'sí'.\n\n"

            f"**INSTRUCCIONES SEGÚN EL ESTADO:**\n"
            f"1.  **Si ESTADO = 'Consultando':**\n"
            f"    - Responde preguntas, da info de productos (descripción, precio, variantes, stock). Si el catálogo dice `images_available: true`, menciona que hay fotos. Si preguntan por algo que no tienes, di que no está disponible y sugiere ALGO SIMILAR DEL CATÁLOGO amablemente.\n"
            f"    - Si el cliente expresa intención de compra (ej: 'quiero X', 'me llevo Y'): Confirma el/los producto(s), cantidad (asume 1 si no se especifica pero pregunta si quiere más), y precio unitario. Calcula el subtotal. Pasa a ESTADO = 'ArmandoCarrito'.\n"
            f"2.  **Si ESTADO = 'ArmandoCarrito':**\n"
            f"    - **RECOMENDACIÓN (SOLO UNA VEZ, SUTIL):** Si acabas de añadir el primer o segundo ítem al carrito, puedes decir algo como: '¡Buena elección! Para acompañar tu [producto principal], ¿qué tal un [producto complementario del catálogo]? O si prefieres, podemos seguir.'\n"
            f"    - **Pregunta '¿Deseas añadir algo más a tu pedido?'**\n"
            f"    - Si dice SÍ o pide otro producto: Añádelo, recalcula subtotal. Mantente en ESTADO = 'ArmandoCarrito' y repite la pregunta de '¿Algo más?'.\n"
            f"    - **Si dice NO (ej: 'no, solo eso', 'eso es todo', 'nada más'):** ¡Perfecto! Informa el costo de envío (COP 5.000) y el TOTAL FINAL. Pasa a ESTADO = 'PidiendoDatos' y empieza a pedir el primer dato.\n"
            f"3.  **Si ESTADO = 'PidiendoDatos':**\n"
            f"    - PIDE LOS DATOS SECUENCIALMENTE (uno por uno, esperando respuesta antes de pedir el siguiente). NO los pidas todos de golpe. Los datos son: Nombre completo, Dirección detallada (con ciudad/barrio), Teléfono de contacto, Método de pago (Nequi, Daviplata, Bancolombia, o contraentrega en efectivo en [tu ciudad/área si aplica]).\n"
            f"    - Revisa el historial para no pedir datos ya dados. Si ya tienes un dato, confírmalo.\n"
            f"    - Cuando tengas todos los datos, pasa a ESTADO = 'ConfirmandoPedidoFinal'.\n"
            f"4.  **Si ESTADO = 'ConfirmandoPedidoFinal':**\n"
            f"    - Resume TODO el pedido: productos (nombre, cantidad, precio unitario), subtotal, envío, TOTAL A PAGAR, y todos los datos de envío del cliente.\n"
            f"    - Pregunta CLARAMENTE: '¿Está todo correcto para confirmar tu pedido? Por favor, dime 'sí' o 'confirmo'.'\n"
            f"    - **SI EL USUARIO CONFIRMA ('sí', 'ok', 'confirmo', 'listo'):** Tu respuesta DEBE terminar con el bloque JSON exacto. El texto conversacional ANTES del JSON debe ser una confirmación entusiasta. Ej: '¡Excelente! Tu pedido ha sido confirmado y ya lo estamos preparando. ¡Muchas gracias por tu compra en Licores El Roble! 🎉'\n"
            f"      ```json\n"
            f"      {{\"order_details\":{{\"name\":\"NOMBRE_COMPLETO\",\"address\":\"DIRECCION_DETALLADA\",\"phone\":\"TELEFONO_CONTACTO\",\"payment_method\":\"METODO_PAGO_ELEGIDO\",\"products\":[{{\"name\":\"NOMBRE_PRODUCTO_1\",\"quantity\":CANTIDAD_NUMERICA,\"price\":PRECIO_UNITARIO_NUMERICO}}],\"total\":TOTAL_PEDIDO_NUMERICO}}}}\n"
            f"      ```\n"
            f"    - **SI EL USUARIO NO CONFIRMA o pide cambios: NO INCLUYAS EL JSON.** Vuelve al estado apropiado (ej. 'ArmandoCarrito' si quiere cambiar productos, o 'PidiendoDatos' si un dato está mal).\n\n"
            f"**GENERAL:**\n"
            f"- Si pide fotos y no se manejó antes, di: 'Claro, ¿de qué producto/variante quieres ver fotos?'. No generes JSON para esto.\n"
            f"- Siempre sé amable. Si no entiendes, pide clarificación."
        )
        # ---- FIN INSTRUCCIONES PARA EL LLM ----

        llm_general_history = current_user_history[-8:] # Enviar últimos 4 intercambios (user/model)
        llm_input_general_conv = llm_general_history + [{"role": "user", "text": order_taking_instructions}]

        logger.info(f"🧠 Gemini (General/Order) - Enviando solicitud...")
        llm_response_general = await ask_gemini_with_history(llm_input_general_conv)

        if isinstance(llm_response_general, str) and (llm_response_general.startswith("GEMINI_API_ERROR:") or llm_response_general.startswith("GEMINI_RESPONSE_ISSUE:")):
            logger.error(f"Error/Problema de Gemini (General/Order): {llm_response_general}")
            gemini_api_error_occurred_message = llm_response_general # Guardar para mensaje final
            # El flujo continuará para ver si hay un mensaje de error guardado de la etapa de imagen, o se usará este.
        else:
            logger.info(f"🧠 Gemini (General/Order) - Raw Response: {llm_response_general}")
        
        # Si hubo un error de API en la llamada general, y no se pudo obtener respuesta
        if gemini_api_error_occurred_message and not (isinstance(llm_response_general, str) and llm_response_general.strip()):
             # Determinar el mensaje de error más específico para el usuario
            final_error_msg_to_user = "Lo siento, estoy experimentando algunas dificultades técnicas en este momento. 🛠️ Por favor, inténtalo de nuevo en unos minutos."
            if "overloaded" in gemini_api_error_occurred_message or "ocupados" in gemini_api_error_occurred_message or "alta demanda" in gemini_api_error_occurred_message:
                 final_error_msg_to_user = "Nuestros sistemas de IA están un poco ocupados en este momento. 😅 ¿Podrías intentarlo de nuevo en un par de minutos, por favor?"
            # ... (más condiciones para otros tipos de errores de gemini_api_error_occurred_message)
            
            send_whatsapp_message(from_number, final_error_msg_to_user)
            model_error_time = datetime.now(timezone.utc)
            current_user_history.append({"role": "model", "text": final_error_msg_to_user, "time": model_error_time.isoformat()})
            await save_message_to_supabase(from_number, "model", final_error_msg_to_user)
            return

        # Procesar la respuesta normal del LLM
        order_data_for_processing, llm_text_response_to_user = extract_order_data(llm_response_general)
        model_response_time = datetime.now(timezone.utc)

        if llm_text_response_to_user and llm_text_response_to_user.strip():
            send_whatsapp_message(from_number, llm_text_response_to_user)
            current_user_history.append({"role": "model", "text": llm_text_response_to_user, "time": model_response_time.isoformat()})
            await save_message_to_supabase(from_number, "model", llm_text_response_to_user)
        elif gemini_api_error_occurred_message: 
            # Si la llamada a imagen tuvo error Y la llamada general no produjo texto, enviar el error de imagen.
            # (Esto es un fallback, idealmente la llamada general siempre produce texto o un error más específico)
            send_whatsapp_message(from_number, "Hubo un problema con el asistente de IA. Intenta de nuevo.")
            # ... (guardar este error en historial/DB)
            return

        # Procesar pedido
        final_order_payload = None
        if order_data_for_processing and isinstance(order_data_for_processing, dict):
            # ... (lógica para obtener final_order_payload igual que antes) ...
            if "order_details" in order_data_for_processing and isinstance(order_data_for_processing["order_details"], dict):
                final_order_payload = order_data_for_processing["order_details"]
            elif all(k in order_data_for_processing for k in ["name", "products", "total"]): 
                final_order_payload = order_data_for_processing


        if final_order_payload:
            logger.info(f"🛍️ Payload de pedido para procesar: {json.dumps(final_order_payload, indent=2)}")
            result_order_processing = await process_order(from_number, final_order_payload)
            status_from_processing = result_order_processing.get("status")
            
            # --- MANEJO DE STATUS DE PROCESS_ORDER (COMO EN TU services/orders.py) ---
            if status_from_processing == "missing":
                missing_fields = result_order_processing.get("fields", [])
                campos_str = ", ".join([f.replace('_',' ').capitalize() for f in missing_fields])
                # Este mensaje lo envía process_order o el LLM ya debería haber pedido estos datos.
                # Si process_order devuelve "missing", es porque el JSON del LLM no fue suficiente
                # para la lógica interna de process_order (que también puede tener user_pending_data).
                # El LLM debería haber sido instruido para obtener todos los datos.
                # Aquí podemos reforzar si process_order lo devuelve.
                send_whatsapp_message(from_number, f"📋 ¡Uy! Parece que al sistema le faltan estos datos para tu pedido: {campos_str}. ¿Podrías confirmarlos o proporcionarlos de nuevo, por favor?")
            elif status_from_processing == "created":
                logger.info(f"✅ Pedido CREADO para {from_number} vía process_order.")
                # El LLM ya envió el mensaje de "Pedido confirmado..."
                # Aquí es donde se hace la recomendación de productos UNA VEZ.
                products_ordered = final_order_payload.get("products", [])
                if products_ordered: # Solo si hay productos en el pedido confirmado
                    # Usar get_recommended_products de tu products.py
                    # Esta función espera una lista de productos del pedido actual.
                    # Asegúrate que `products_ordered` tenga el formato que espera `get_recommended_products`.
                    # Ej: `[{"name": "Producto A", "quantity": 1, "price": 100}, ...]`
                    recommendations = await get_recommended_products(products_ordered) 
                    if recommendations:
                        rec_text_parts = [f"  - {r['name']} (COP {r.get('price', 0):,})" for r in recommendations]
                        # Enviar como un mensaje separado para no interrumpir la confirmación del pedido.
                        send_whatsapp_message(from_number, 
                            f"✨ ¡Ya que estamos! Para complementar tu pedido, también te podrían interesar:\n"
                            f"{chr(10).join(rec_text_parts)}\n\n"
                            "Si algo te llama la atención para una próxima compra, ¡me avisas! 😉"
                        )
            elif status_from_processing == "updated":
                logger.info(f"♻️ Pedido ACTUALIZADO para {from_number} vía process_order.")
                # El LLM ya debería haber enviado un mensaje apropiado.
            elif status_from_processing == "error":
                error_detail = result_order_processing.get('error', 'Error desconocido al procesar el pedido.')
                logger.error(f"❌ Error desde process_order para {from_number}: {error_detail}")
                send_whatsapp_message(from_number, "¡Ups! Tuvimos un inconveniente técnico al registrar tu pedido en nuestro sistema final. 🛠️ Un asesor se pondrá en contacto contigo si es necesario. ¡Gracias por tu paciencia!")
            else: # Estado no manejado o inesperado
                logger.warning(f"⚠️ Estado no manejado de process_order: '{status_from_processing}' para {from_number}")
        
        elif not (llm_text_response_to_user and llm_text_response_to_user.strip()) and not gemini_api_error_occurred_message:
            # Si no hubo JSON de pedido, ni texto conversacional, Y TAMPOCO hubo un error de API previo
            logger.error(f"LLM no proporcionó respuesta útil (ni texto, ni JSON, ni error API previo) para: '{user_raw_text}'")
            send_whatsapp_message(from_number, "¡Uy! Parece que me enredé un poquito con tu último mensaje. 😅 ¿Podrías intentar decírmelo de otra forma, porfa?")

    except Exception as e_global:
        logger.critical(f"❌ [ERROR CRÍTICO GLOBAL en handle_user_message]: {e_global}", exc_info=True)
        final_fallback_message = gemini_api_error_occurred_message if gemini_api_error_occurred_message \
                               else "¡Ups! Algo no salió bien de mi lado y no pude procesar tu solicitud. 🤖 Un técnico ya fue notificado. Por favor, intenta de nuevo en un momento."
        try:
            send_whatsapp_message(from_number, final_fallback_message)
            model_fallback_time = datetime.now(timezone.utc)
            current_user_history = user_histories.setdefault(from_number, [])
            current_user_history.append({"role": "model", "text": final_fallback_message, "time": model_fallback_time.isoformat()})
            await save_message_to_supabase(from_number, "model", final_fallback_message)
        except Exception as e_send_fallback:
            logger.error(f"Falló el envío del mensaje de fallback de error global a {from_number}: {e_send_fallback}")