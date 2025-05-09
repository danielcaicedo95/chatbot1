# app/services/conversation.py

from datetime import datetime, timezone
import json
import re
import traceback
from difflib import get_close_matches
import logging

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history # Importamos la función actualizada
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order
from app.utils.extractors import extract_order_data

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Campos requeridos para guiar al LLM en la conversación de toma de pedidos.
REQUIRED_CONVERSATIONAL_ORDER_FIELDS = ["name", "address", "phone", "payment_method"]


# --- Funciones Auxiliares de Catálogo y Matching ---
# (Estas funciones: build_structured_catalog_for_logic, format_catalog_for_llm_context,
#  match_target_in_catalog_for_images, se mantienen igual que en la respuesta anterior
#  ya que su lógica interna no depende directamente de los cambios en el cliente Gemini,
#  sino de la estructura de tus productos.)

def build_structured_catalog_for_logic(productos_list: list) -> list:
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
                    "id": v_data["id"], 
                    "display_label": ", ".join(display_label_parts),
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
    # Difuso (simplificado para brevedad, puedes usar el más complejo si prefieres)
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
    from_number: str,
    user_raw_text: str,
    current_history: list,
    structured_catalog_data: list
) -> bool:
    try:
        catalog_summary_for_llm = [{"name": p["name"], "variants": [v["display_label"] for v in p.get("variants", [])]} for p in structured_catalog_data]
        image_intent_prompt = {
            "user_request": user_raw_text, "available_products_summary": catalog_summary_for_llm,
            "task": "Analiza 'user_request'. Si pide imágenes/fotos, responde JSON: {\"want_images\": true, \"target\": \"nombre producto/variante\"}. Si no, {\"want_images\": false}. Si el target no es claro, añade 'clarification_needed': 'mensaje'.",
            "examples": [{"user": "fotos tequila?", "bot_json": {"want_images": True, "target": "tequila"}}, {"user": "foto porfa", "bot_json": {"want_images": True, "target": "producto contexto", "clarification_needed": "¡Claro! ¿De qué producto?"}}]
        }
        llm_input = current_history[-3:] + [{"role": "user", "text": json.dumps(image_intent_prompt, ensure_ascii=False)}]
        
        logger.info(f"🧠 Gemini (Image Intent) - Enviando solicitud...")
        llm_response_text = await ask_gemini_with_history(llm_input)

        # --- MANEJO DE ERRORES DE GEMINI ---
        if isinstance(llm_response_text, str) and (llm_response_text.startswith("GEMINI_API_ERROR:") or llm_response_text.startswith("GEMINI_RESPONSE_ISSUE:")):
            logger.error(f"Error/Problema de Gemini (Image Intent): {llm_response_text}")
            # No enviaremos mensaje específico aquí, handle_user_message lo hará si es un error general.
            # Si es un GEMINI_RESPONSE_ISSUE que no es crítico, podríamos querer continuar,
            # pero por ahora lo trataremos como una falla en obtener la intención clara.
            return False # Indica que la intención de imagen no se pudo determinar claramente.

        logger.info(f"🧠 Gemini (Image Intent) - Raw Response: {llm_response_text}")
        # --- FIN MANEJO DE ERRORES DE GEMINI ---

        json_match = re.search(r"\{[\s\S]*\}", llm_response_text)
        if not json_match:
            logger.warning("No JSON en respuesta LLM para intención de imagen. Asumiendo NO quiere imágenes.")
            return False 
        
        try:
            action = json.loads(json_match.group())
        except json.JSONDecodeError:
            logger.error(f"Error decodificando JSON intención imagen. Respuesta: {json_match.group()}")
            return False # Falla segura

        if not action.get("want_images"):
            return False 

        if action.get("clarification_needed") and isinstance(action["clarification_needed"], str):
            send_whatsapp_message(from_number, action["clarification_needed"])
            return True 

        target_description = action.get("target")
        if not target_description or not isinstance(target_description, str):
            send_whatsapp_message(from_number, "¡Claro! Quieres fotos. ¿De qué producto o variante te gustaría verlas? 🤔")
            return True 

        matched_product_cat_obj, matched_variant_cat_obj = match_target_in_catalog_for_images(
            structured_catalog_data, target_description
        )

        if not matched_product_cat_obj:
            send_whatsapp_message(from_number, f"Lo siento, no encontré '{target_description}' para mostrarte imágenes. 😔")
            return True

        # ... (Lógica para recopilar image_urls_to_send y display_name_for_caption
        #      basada en matched_product_cat_obj y matched_variant_cat_obj.
        #      Esta parte se mantiene igual que en la respuesta anterior detallada) ...
        image_urls_to_send = []
        display_name_for_caption = matched_product_cat_obj["name"]
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
            return True 

        send_whatsapp_message(from_number, f"¡Claro! Aquí tienes las imágenes de *{display_name_for_caption}*:")
        for img_url in image_urls_to_send:
            try:
                send_whatsapp_image(from_number, img_url, caption=display_name_for_caption)
            except Exception as e_img:
                logger.error(f"❌ Error enviando imagen {img_url}: {e_img}", exc_info=True)
        return True
    except Exception as e_img_handler:
        logger.error(f"⚠️ Error en handle_image_request_logic: {e_img_handler}", exc_info=True)
        return False


# --- Flujo Principal de Mensajes ---
async def handle_user_message(body: dict):
    # Variable para almacenar el mensaje de error de Gemini si ocurre
    gemini_error_message_to_user = None
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
        await save_message_to_supabase(from_number, "user", user_raw_text) # Sin timestamp

        productos_db_data = await get_all_products()
        if not productos_db_data:
            logger.error("CRÍTICO: No se pudieron obtener productos.")
            send_whatsapp_message(from_number, "Lo siento, tenemos problemas para acceder al catálogo ahora. 🛠️ Intenta más tarde.")
            return
        
        s_catalog_logic = build_structured_catalog_for_logic(productos_db_data)

        # 1. Intentar manejar como solicitud de imagen
        llm_response_for_images = await ask_gemini_with_history(
            current_user_history[-3:] + [{"role": "user", "text": json.dumps({ # Simplified prompt for brevity here
                "user_request": user_raw_text, "task": "Si pide fotos, JSON: {\"want_images\": true, \"target\": \"producto\"}. Si no, {\"want_images\": false}."
            }, ensure_ascii=False)}]
        )

        image_request_handled_internally = False
        if isinstance(llm_response_for_images, str) and (llm_response_for_images.startswith("GEMINI_API_ERROR:") or llm_response_for_images.startswith("GEMINI_RESPONSE_ISSUE:")):
            logger.error(f"Error de Gemini (Image Intent Check): {llm_response_for_images}")
            # Guardar este error para posible mensaje al final si no hay otra respuesta
            gemini_error_message_to_user = "Estoy teniendo algunas dificultades técnicas con el asistente de IA. 🛠️"
            if "overloaded" in llm_response_for_images or "ocupados" in llm_response_for_images:
                gemini_error_message_to_user = "Nuestros sistemas de IA están un poco ocupados. 😅 Intenta en unos minutos."
            # No retornamos aún, intentaremos el flujo general.
        else:
            # Si no hubo error de API, procesamos la respuesta para la intención de imagen
            image_request_handled_internally = await handle_image_request_logic(
                from_number, user_raw_text, current_user_history, s_catalog_logic # Pasamos la respuesta del LLM
            )
            if image_request_handled_internally:
                logger.info("Solicitud de imagen manejada por handle_image_request_logic. Finalizando.")
                return


        # 2. Si no fue imagen (o falló la detección de intención de imagen pero no fue error de API),
        #    procesar como conversación general/pedido
        logger.info(f"Procesando como mensaje general/pedido para {from_number}")

        catalog_for_llm_prompt = format_catalog_for_llm_context(s_catalog_logic)
        order_taking_instructions = (
            f"Eres 'VendiBot', vendedor amigable de licorera 'Licores El Roble'. Tono cercano, emojis. Ayuda y toma pedidos.\n"
            f"USUARIO: \"{user_raw_text}\"\n"
            f"CATÁLOGO:\n{json.dumps(catalog_for_llm_prompt, indent=2, ensure_ascii=False)}\n\n"
            f"**INSTRUCCIONES PEDIDO:**\n"
            f"1. **Intención Compra:** Si quiere comprar, confirma productos/cantidades/precios. Subtotal.\n"
            f"2. **Envío:** Cuesta COP 5.000. Sumar al total.\n"
            f"3. **¿Algo Más?:** Pregunta. Si sí, vuelve al paso 1. Recomienda 1 producto sutilmente.\n"
            f"4. **Pedir Datos (SI DICE 'NO' a 'Algo Más?'):** PIDE SECUENCIALMENTE: Nombre, Dirección, Teléfono, Método Pago (Nequi, Daviplata, Bancolombia, contraentrega en [tu ciudad]). Revisa historial para no repetir.\n"
            f"5. **Confirmación Final y JSON (TODOS LOS DATOS DEL PASO 4 Y CARRITO ARMADO):\n**"
            f"   - Resume: productos, cantidades, precios, subtotal, envío, total, datos cliente.\n"
            f"   - Pregunta: '¿Todo correcto para confirmar?'.\n"
            f"   - **SI USUARIO CONFIRMA ('sí', 'ok'), ENTONCES Y SÓLO ENTONCES**, termina tu respuesta con JSON exacto (sin 'json' antes):\n"
            f"     ```json\n"
            f"     {{\"order_details\":{{\"name\":\"NOMBRE\",\"address\":\"DIRECCION\",\"phone\":\"TELEFONO\",\"payment_method\":\"PAGO\",\"products\":[{{\"name\":\"PROD1\",\"quantity\":1,\"price\":10000}}],\"total\":15000}}}}\n"
            f"     ```\n"
            f"   - Tu texto ANTES del JSON debe ser una confirmación. Ej: '¡Perfecto! Pedido confirmado. 🎉'\n"
            f"   - **SI NO CONFIRMA O PIDE CAMBIOS, NO INCLUYAS EL JSON.** Sigue la conversación.\n"
            f"6. **Otros:** Si solo pregunta, responde amablemente. Si agotado, sugiere alternativas. Si pide fotos y no se manejó antes, di 'Claro, ¿de qué producto quieres ver fotos?'."
        )

        llm_general_history = current_user_history[-8:]
        llm_input_general_conv = llm_general_history + [{"role": "user", "text": order_taking_instructions}]

        logger.info(f"🧠 Gemini (General/Order) - Enviando solicitud...")
        llm_response_general = await ask_gemini_with_history(llm_input_general_conv)

        # --- MANEJO DE ERRORES DE GEMINI ---
        if isinstance(llm_response_general, str) and (llm_response_general.startswith("GEMINI_API_ERROR:") or llm_response_general.startswith("GEMINI_RESPONSE_ISSUE:")):
            logger.error(f"Error/Problema de Gemini (General/Order): {llm_response_general}")
            # Actualizar el mensaje de error si este es más específico
            gemini_error_message_to_user = "Lo siento, estoy experimentando algunas dificultades técnicas en este momento. 🛠️ Por favor, inténtalo de nuevo en unos minutos."
            if "overloaded" in llm_response_general or "ocupados" in llm_response_general or "alta demanda" in llm_response_general:
                 gemini_error_message_to_user = "Nuestros sistemas de IA están un poco ocupados en este momento. 😅 ¿Podrías intentarlo de nuevo en un par de minutos, por favor?"
            elif "conexión" in llm_response_general:
                gemini_error_message_to_user = "Parece que hay un problema de conexión con nuestros servicios de IA. Estoy trabajando en ello. 📡"
            elif "solicitud" in llm_response_general: 
                gemini_error_message_to_user = "Hubo un pequeño inconveniente al procesar tu solicitud con nuestro asistente de IA. 🤔"
            
            send_whatsapp_message(from_number, gemini_error_message_to_user)
            model_error_time = datetime.now(timezone.utc)
            current_user_history.append({"role": "model", "text": gemini_error_message_to_user, "time": model_error_time.isoformat()})
            await save_message_to_supabase(from_number, "model", gemini_error_message_to_user)
            return 
        # --- FIN MANEJO DE ERRORES DE GEMINI ---

        logger.info(f"🧠 Gemini (General/Order) - Raw Response: {llm_response_general}")
        
        order_data_for_processing, llm_text_response_to_user = extract_order_data(llm_response_general)
        model_response_time = datetime.now(timezone.utc)

        if llm_text_response_to_user and llm_text_response_to_user.strip():
            send_whatsapp_message(from_number, llm_text_response_to_user)
            current_user_history.append({"role": "model", "text": llm_text_response_to_user, "time": model_response_time.isoformat()})
            await save_message_to_supabase(from_number, "model", llm_text_response_to_user)
        elif gemini_error_message_to_user: # Si hubo error en Image Intent y no hubo respuesta general
            send_whatsapp_message(from_number, gemini_error_message_to_user)
            model_error_time = datetime.now(timezone.utc)
            current_user_history.append({"role": "model", "text": gemini_error_message_to_user, "time": model_error_time.isoformat()})
            await save_message_to_supabase(from_number, "model", gemini_error_message_to_user)
            return

        # Procesar pedido
        final_order_payload = None
        if order_data_for_processing and isinstance(order_data_for_processing, dict):
            if "order_details" in order_data_for_processing and isinstance(order_data_for_processing["order_details"], dict):
                final_order_payload = order_data_for_processing["order_details"]
            elif all(k in order_data_for_processing for k in ["name", "products", "total"]): 
                final_order_payload = order_data_for_processing

        if final_order_payload:
            logger.info(f"🛍️ Payload de pedido para procesar: {json.dumps(final_order_payload, indent=2)}")
            result_order_processing = await process_order(from_number, final_order_payload) # Tu `process_order`
            status_from_processing = result_order_processing.get("status")
            
            if status_from_processing == "missing":
                missing_fields = result_order_processing.get("fields", [])
                campos_str = ", ".join([f.replace('_',' ') for f in missing_fields])
                send_whatsapp_message(from_number, f"📋 ¡Casi listo! Para completar tu pedido, necesitamos: {campos_str}. ¿Podrías proporcionarlos?")
            elif status_from_processing == "created":
                logger.info(f"✅ Pedido CREADO para {from_number} vía process_order.")
                products_ordered = final_order_payload.get("products", [])
                if products_ordered:
                    recommendations = await get_recommended_products(products_ordered)
                    if recommendations:
                        rec_text_parts = [f"- {r['name']} (COP {r.get('price', 0):,})" for r in recommendations]
                        send_whatsapp_message(from_number, f"✨ ¡Para complementar tu pedido! También te podrían interesar:\n{chr(10).join(rec_text_parts)}\n¿Te animas por alguno más? 😉")
            elif status_from_processing == "updated":
                logger.info(f"♻️ Pedido ACTUALIZADO para {from_number} vía process_order.")
            elif status_from_processing == "error":
                logger.error(f"❌ Error desde process_order: {result_order_processing.get('error', 'Error desconocido')}")
                send_whatsapp_message(from_number, "¡Ups! Tuvimos un inconveniente técnico al registrar tu pedido. 🛠️ Inténtalo de nuevo o contacta a soporte.")
            else:
                logger.warning(f"⚠️ Estado no manejado de process_order: '{status_from_processing}' para {from_number}")
        
        elif not llm_text_response_to_user or not llm_text_response_to_user.strip() and not gemini_error_message_to_user:
            logger.error(f"LLM no proporcionó respuesta útil para: '{user_raw_text}' y no hubo error de API previo.")
            send_whatsapp_message(from_number, "¡Uy! Parece que me enredé un poquito. 😅 ¿Podrías decírmelo de otra forma?")

    except Exception as e_global:
        logger.critical(f"❌ [ERROR CRÍTICO GLOBAL en handle_user_message]: {e_global}", exc_info=True)
        final_fallback_message = gemini_error_message_to_user if gemini_error_message_to_user \
                               else "¡Ups! Algo no salió bien de mi lado y no pude procesar tu solicitud. 🤖 Un técnico ya fue notificado. Por favor, intenta de nuevo en un momento."
        try:
            send_whatsapp_message(from_number, final_fallback_message)
            # Considerar guardar este error en Supabase también
            model_fallback_time = datetime.now(timezone.utc)
            current_user_history = user_histories.setdefault(from_number, []) # Asegurar que exista
            current_user_history.append({"role": "model", "text": final_fallback_message, "time": model_fallback_time.isoformat()})
            await save_message_to_supabase(from_number, "model", final_fallback_message)
        except Exception as e_send_fallback:
            logger.error(f"Falló el envío del mensaje de fallback de error global a {from_number}: {e_send_fallback}")