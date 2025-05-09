# app/services/conversation.py

from datetime import datetime
import json
import re
import traceback
from difflib import get_close_matches

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image # Asumimos que son async como en tu primer código
from app.services.supabase import save_message_to_supabase # Asumimos que es async
from app.services.products import get_all_products, get_recommended_products # Asumimos que son async
from app.services.orders import process_order # Asumimos que es async
from app.utils.extractors import extract_order_data

# Funciones auxiliares (extraídas del cuerpo principal)

def extract_labels(obj):
    """Extrae todas las cadenas de una estructura anidada (diccionario/lista)."""
    labels = []
    def _extract(o):
        if isinstance(o, dict):
            for v in o.values():
                _extract(v)
        elif isinstance(o, list):
            for v in o:
                _extract(v)
        elif isinstance(o, str):
            labels.append(o)
    _extract(obj)
    return labels

def build_catalog(productos_list):
    """Construye una estructura de catálogo para el LLM y búsqueda local."""
    catalog = []
    for p in productos_list:
        variants_data = []
        # Recopilar imágenes por variante si hay ID, o generales si no
        # También recopilar variant_label de las imágenes si está presente
        images_by_variant_id = {}
        images_general = []
        variant_labels_from_images = {}

        for img in p.get("product_images", []):
            if img.get("variant_id") is not None:
                images_by_variant_id.setdefault(img["variant_id"], []).append(img["url"])
                if img.get("variant_label"):
                     # Normalizar la etiqueta de la imagen para la búsqueda
                    variant_labels_from_images[img["variant_id"]] = img["variant_label"].strip().lower()
            else:
                images_general.append(img["url"])

        for v in p.get("product_variants", []):
            opts = v.get("options", {})
            if not opts:
                continue

            # Asegurar que el valor de la opción se tome y se ponga en minúscula para búsqueda
            option_key = next(iter(opts.keys()))
            option_value = str(opts[option_key]).strip().lower() # Convertir a str y luego lower

            # Construir la etiqueta de visualización para el LLM
            display_label_parts = []
            catalog_variant_label_parts = [] # Para matching, ej: "option:amarillo"
            for k_opt, v_opt in opts.items():
                 display_label_parts.append(f"{k_opt}:{v_opt}")
                 catalog_variant_label_parts.append(f"{k_opt}:{str(v_opt).strip().lower()}")


            # Buscar imágenes asociadas a esta variante (por ID o por label de imagen)
            variant_images = images_by_variant_id.get(v["id"], [])
            
            # Si no se encontraron imágenes por ID, intentar por variant_label si existe en las imágenes
            # Esto es un fallback por si la DB tiene variant_label pero no variant_id en la tabla de images (o inconsistencias)
            if not variant_images and v["id"] in variant_labels_from_images:
                 target_v_label_lower = variant_labels_from_images[v["id"]]
                 # Buscar imágenes que coincidan con esta etiqueta (a nivel de producto principal)
                 variant_images = [
                     img["url"] for img in p.get("product_images", [])
                     if img.get("variant_label") and img["variant_label"].strip().lower() == target_v_label_lower
                 ]
                 # Eliminar duplicados si una imagen por error tuviera la misma label y no ID asignado
                 variant_images = list(set(variant_images))


            variants_data.append({
                "id": v["id"],
                "value": option_value, # ej: "amarillo" (para matching simple)
                "display_label": ", ".join(display_label_parts), # ej: "Color:Amarillo" (para mostrar al LLM)
                "catalog_variant_label": ",".join(catalog_variant_label_parts), # ej: "color:amarillo" (para matching con labels de imágenes)
                "images": variant_images # Imágenes encontradas para esta variante
            })

        catalog.append({
            "name": p["name"],
            "variants": variants_data,
            "images": images_general # Imágenes generales del producto
        })
    return catalog


def match_target_in_catalog(catalog_list, productos_list, target_str):
    """Intenta encontrar un producto o variante en el catálogo basado en el string objetivo."""
    if not target_str:
        return None, None

    target_str = target_str.strip().lower()

    # Búsqueda exacta/cercana en nombres de productos y valores/labels de variantes
    all_choices = []
    choice_to_item_map = {}

    for entry in catalog_list:
        prod_name_lower = entry["name"].strip().lower()
        all_choices.append(prod_name_lower)
        choice_to_item_map[prod_name_lower] = (entry["name"], None) # Mapea a nombre de producto

        for v_catalog in entry["variants"]:
            variant_value_lower = v_catalog["value"] # ej: "amarillo"
            variant_display_label_lower = v_catalog["display_label"].strip().lower() # ej: "color:amarillo"

            # Añadir el valor simple (ej: "amarillo")
            all_choices.append(variant_value_lower)
            choice_to_item_map[variant_value_lower] = (entry["name"], v_catalog) # Mapea a obj de variante del catálogo

            # Añadir la etiqueta completa (ej: "color:amarillo")
            all_choices.append(variant_display_label_lower)
            choice_to_item_map[variant_display_label_lower] = (entry["name"], v_catalog)

            # Añadir combinación nombre producto + valor variante (ej: "tequila jose cuervo amarillo")
            combined_name_value = f"{prod_name_lower} {variant_value_lower}"
            all_choices.append(combined_name_value)
            choice_to_item_map[combined_name_value] = (entry["name"], v_catalog)

            # Añadir combinación nombre producto + etiqueta variante (ej: "tequila jose cuervo color:amarillo")
            combined_name_label = f"{prod_name_lower} {variant_display_label_lower}"
            all_choices.append(combined_name_label)
            choice_to_item_map[combined_name_label] = (entry["name"], v_catalog)


    # Usar get_close_matches para encontrar la mejor coincidencia
    # Aumentar cutoff para ser más estricto si es necesario, o bajarlo para ser más flexible.
    # n=1, busca solo el mejor match.
    best_matches = get_close_matches(target_str, all_choices, n=1, cutoff=0.6) # Ajustar cutoff según necesidad
    
    if best_matches:
        match_key = best_matches[0]
        matched_prod_name, matched_item = choice_to_item_map[match_key]

        # Encontrar el objeto de producto original completo
        prod_obj = next((p for p in productos_list if p["name"] == matched_prod_name), None)
        if not prod_obj:
            return None, None # No se encontró el producto original

        # Si el match fue una variante, devolver el producto y el objeto de variante del *catálogo* (que incluye info de imágenes)
        if isinstance(matched_item, dict): # Es un objeto de variante del catálogo
            return prod_obj, matched_item
        # Si el match fue un producto, devolver solo el producto
        elif matched_item is None:
             return prod_obj, None

    return None, None # No se encontró ninguna coincidencia


async def handle_image_request_logic(from_number: str, raw_text: str, user_history: list, productos: list):
    """
    Intenta detectar y manejar una solicitud de imagen.
    Retorna True si una solicitud de imagen fue detectada Y manejada (enviando imágenes,
    mensaje de no encontradas, o pidiendo clarificación). Retorna False si no fue una
    solicitud de imagen según el LLM, o si ocurrió un error antes de enviar un mensaje al usuario.
    """
    try:
        catalog_data = build_catalog(productos)
        # Simplificamos el catálogo para el LLM, solo nombres y etiquetas de variante
        catalog_for_llm = [{"name": p["name"], "variants": [v["display_label"] for v in p["variants"]]} for p in catalog_data]

        prompt_obj = {
            "user_request": raw_text,
            "catalog_summary": catalog_for_llm, # Usamos el resumen para el LLM
            "instructions": [
                "Tu tarea es detectar si el usuario quiere ver una imagen de un producto o variante mencionado en su solicitud o historial.",
                "Analiza la 'user_request' y el 'catalog_summary'.",
                "Si el usuario quiere una imagen, responde **SOLO** con un JSON plano (sin Markdown ni texto adicional, solo las llaves) así:",
                "  {\"want_images\": true, \"target\": \"texto exacto mencionado por el usuario sobre el producto o variante, ej: 'Tequila Jose Cuervo amarillo', 'Aguardiente Nariño azul', 'Tequila', 'amarillo' si el contexto es claro\", \"clarification_needed\": null}",
                "Si no estás seguro de qué producto/variante quiere imagen, pero la intención de ver imágenes es clara, responde:",
                "  {\"want_images\": true, \"target\": null, \"clarification_needed\": \"Por favor, especifica de qué producto o variante quieres la imagen.\"}",
                "Si NO quiere imágenes, responde **SOLO** con:",
                "  {\"want_images\": false, \"target\": null, \"clarification_needed\": null}",
                "Ejemplos de JSONs esperados:",
                "- '¿Tienes fotos del tequila?' -> {\"want_images\": true, \"target\": \"tequila\", \"clarification_needed\": null}",
                "- 'Muéstrame el tequila amarillo' -> {\"want_images\": true, \"target\": \"tequila amarillo\", \"clarification_needed\": null}",
                "- 'Quiero ver cómo es' (contexto no claro) -> {\"want_images\": true, \"target\": null, \"clarification_needed\": \"Por favor, especifica de qué producto o variante quieres la imagen.\"}",
                "- 'Quiero comprar tequila' -> {\"want_images\": false, \"target\": null, \"clarification_needed\": null}", # Intención es comprar, no ver imagen
                "Asegúrate de que tu respuesta sea *solo* el JSON."
            ]
        }

        # Usar solo una parte relevante del historial para el análisis de imagen si es muy largo
        hist_for_image_llm = [m for m in user_history if m["role"] in ("user", "model")][-5:] # Últimos 5 turnos

        llm_input_messages = hist_for_image_llm + [{"role": "user", "text": json.dumps(prompt_obj, ensure_ascii=False)}]

        # print(f"🧠 Enviando a Gemini para análisis de imagen...") # Opcional: log detallado
        llm_resp_text = await ask_gemini_with_history(llm_input_messages)
        # print(f"🧠 Respuesta de Gemini (imagen): {llm_resp_text}") # Opcional: log detallado

        # Buscar el JSON en la respuesta
        match = re.search(r"\{[\s\S]*\}", llm_resp_text)
        if not match:
            print(f"⚠️ No se encontró JSON en la respuesta del modelo para imágenes. Respuesta cruda: {llm_resp_text[:200]}...")
            return False # No se pudo interpretar, dejar que el flujo principal continúe

        try:
            action = json.loads(match.group())
        except json.JSONDecodeError:
            print(f"⚠️ No se pudo parsear el JSON de la respuesta del modelo para imágenes: {match.group()}. Respuesta cruda: {llm_resp_text[:200]}...")
            return False # JSON inválido, dejar que el flujo principal continúe


        if not action.get("want_images", False):
            return False # El LLM determinó que no es una solicitud de imagen

        # Si llegamos aquí, el LLM cree que el usuario quiere imágenes
        print("✅ LLM detectó solicitud de imágenes.")

        if action.get("clarification_needed"):
            await send_whatsapp_message(from_number, action["clarification_needed"])
            return True # Solicitud de imagen manejada (pidiendo clarificación)

        target_description = action.get("target")
        if not target_description:
             # Si want_images=true pero target es null y no hay clarification_needed (caso inesperado)
             print("⚠️ LLM indicó want_images=true pero target=null y no clarification_needed.")
             await send_whatsapp_message(from_number, "No estoy seguro de qué imágenes mostrar. ¿Podrías ser más específico?")
             return True # Consideramos manejado este caso

        matched_product, matched_variant_catalog_obj = match_target_in_catalog(catalog_data, productos, target_description)

        if not matched_product:
            await send_whatsapp_message(from_number, f"Lo siento, no encontré el producto '{target_description}' en nuestro catálogo para mostrarte imágenes.")
            return True # Solicitud de imagen manejada (informando que no se encontró)

        image_urls_to_send = []
        display_name = matched_product["name"]

        if matched_variant_catalog_obj: # Si se encontró una variante específica en el catálogo
            display_name = f"{matched_product['name']} ({matched_variant_catalog_obj['display_label']})"
            image_urls_to_send = matched_variant_catalog_obj.get("images", []) # Usamos las imágenes ya recopiladas en build_catalog
            # print(f"🖼️ Imágenes encontradas en catálogo para variante '{display_name}': {image_urls_to_send}")


        # Si no se encontraron imágenes específicas de variante O si no se pidió variante, buscar imágenes generales del producto
        if not image_urls_to_send:
            catalog_entry = next((ce for ce in catalog_data if ce["name"] == matched_product["name"]), None)
            if catalog_entry:
                image_urls_to_send = catalog_entry.get("images", []) # Usamos las imágenes generales de build_catalog
            # print(f"🖼️ Imágenes encontradas en catálogo para producto general '{matched_product['name']}': {image_urls_to_send}")


        if not image_urls_to_send:
            msg_no_img = f"No tenemos imágenes disponibles para *{display_name}* en este momento. ¿Te puedo ayudar con algo más?"
            await send_whatsapp_message(from_number, msg_no_img)
            return True # Solicitud de imagen manejada (informando que no hay imágenes)

        # Si hay imágenes, enviarlas
        await send_whatsapp_message(from_number, f"¡Claro! Aquí tienes las imágenes de *{display_name}*:")
        images_sent_count = 0
        for img_url in image_urls_to_send:
            try:
                # print(f"🖼️ Enviando imagen: {img_url}")
                await send_whatsapp_image(from_number, img_url, caption=display_name)
                images_sent_count += 1
            except Exception as e:
                print(f"❌ Error enviando imagen {img_url}: {e}")
                # Continúa enviando las otras imágenes
        
        if images_sent_count > 0:
            return True # Solicitud de imagen manejada (se enviaron imágenes)
        else:
             # Si Gemini dijo que sí, había un target, encontramos el producto, había URLs, pero falló el envío de todas
             # Podríamos considerar esto como "manejado" el intento, aunque fallara el envío
             print("⚠️ No se pudo enviar ninguna imagen a pesar de haberlas encontrado.")
             await send_whatsapp_message(from_number, f"Tuve un problema enviando las imágenes de *{display_name}*. Por favor, inténtalo de nuevo.")
             return True # Consideramos manejado el intento fallido de envío

    except Exception:
        print(f"⚠️ Error en handle_image_request_logic:\n{traceback.format_exc()}")
        # Si hay un error interno, retornamos False para que el flujo principal continúe
        return False


def build_order_context(productos_list):
    """Construye el texto del catálogo para incluir en el prompt del LLM para pedidos."""
    contexto_lines = []
    for p in productos_list:
        try:
            variantes = p.get("product_variants") or []

            if not variantes: # Asumiendo que price y stock están en el producto principal si no hay variantes
                price_info = f"COP {p.get('price', 0)}" if p.get('price') is not None else "Precio no disponible"
                stock_info = f"(stock {p.get('stock', 'N/A')})" if p.get('stock') is not None else "(stock no disponible)"
                line = f"- {p['name']}: {price_info} {stock_info}"
            else:
                line = f"- {p['name']}:"
                opts = []
                for v_prod in variantes:
                    price = v_prod.get("price", p.get("price")) # Fallback al precio del producto
                    stock = v_prod.get("stock", "N/A")
                    price_info = f"COP {price}" if price is not None else "Precio no disponible"
                    stock_info = f"(stock {stock})" if stock is not None else "(stock no disponible)"

                    options_str_parts = []
                    for k_opt, v_opt_val in v_prod.get("options", {}).items():
                         options_str_parts.append(f"{k_opt}:{v_opt_val}")
                    options_str = ", ".join(options_str_parts)
                    opts.append(f"    • {options_str} — {price_info} {stock_info}")
                
                if opts: # Solo añadir si hay opciones procesadas
                   line += "\n" + "\n".join(opts)
                else: # Si no hay variantes procesables, mostrar info base del producto si existe
                    price_info = f"COP {p.get('price', 0)}" if p.get('price') is not None else "Precio base no disponible"
                    stock_info = f"(stock base {p.get('stock', 'N/A')})" if p.get('stock') is not None else "(stock base no disponible)"
                    line += f" ({price_info}, {stock_info})"


            # Mencionar si hay imágenes generales o de variante
            total_images = len(p.get("product_images", []))
            if total_images > 0:
                 # Podríamos refinar esto para decir si hay generales vs. de variante, pero total es suficiente
                 line += f"\n    🖼️ Imágenes disponibles: Sí ({total_images})" # No indicar la cantidad si no queremos dar esa pista

            contexto_lines.append(line)
        except Exception as e:
            print(f"⚠️ Error construyendo línea de catálogo para {p.get('name', 'Producto Desconocido')}: {e}")
    
    if not contexto_lines:
        return "🛍️ Catálogo actual: No hay productos disponibles en este momento."

    return "🛍️ Catálogo actual (puedes preguntar por precios o pedirme fotos de cualquiera):\n\n" + "\n\n".join(contexto_lines)


# Función principal para manejar el mensaje del usuario
async def handle_user_message(body: dict):
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages")

        if not messages:
            # No es un mensaje del usuario (ej: un cambio de estado). Ignorar.
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")

        if not raw_text or not from_number:
            # Mensaje vacío o número desconocido. Ignorar.
            return

        print(f"📥 Mensaje recibido de {from_number}: {raw_text}")

        # 1. Guardar mensaje de usuario y añadir al historial
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        try:
            await save_message_to_supabase(from_number, "user", raw_text)
            print("💾 Mensaje de usuario guardado en Supabase.")
        except Exception as e:
             print(f"❌ Error guardando mensaje de usuario en Supabase: {e}")
             # Continuar aunque falle la BD


        # 2. Obtener catálogo de productos
        productos = await get_all_products()
        if not productos:
            print("⚠️ No se pudieron obtener los productos del servicio.")
            await send_whatsapp_message(from_number, "Lo siento, estoy teniendo problemas para acceder a nuestro catálogo en este momento. Por favor, inténtalo más tarde.")
            return # No podemos hacer nada sin productos

        # 3. Intentar manejar como solicitud de imagen primero
        # Pasamos el historial actual completo, raw_text, from_number y productos
        image_request_was_handled = await handle_image_request_logic(from_number, raw_text, user_histories[from_number], productos)

        # Si la solicitud de imagen fue manejada (se envió un mensaje al usuario, sea imágenes, error o clarificación), terminamos aquí.
        if image_request_was_handled:
            print("✅ Solicitud de imagen manejada. Finalizando procesamiento.")
            return

        # 4. Si no fue solicitud de imagen o no se manejó, procesar como mensaje general/pedido
        print("📝 No fue solicitud de imagen o no se manejó. Procesando como mensaje general/pedido.")

        instrucciones_gemini = (
            f"Historial de conversación con el usuario:\n"
            # Nota: El historial completo se pasa a ask_gemini_with_history. Estas instrucciones son una adición.
            f"\n\nMensaje actual del usuario: {raw_text}\n\n"
            f"{build_order_context(productos)}\n\n" # Incluir el catálogo para contexto de pedido
            "INSTRUCCIONES PARA EL BOT:\n"
            "1. Actúa como un vendedor amigable y servicial para una licorera. Usa emojis relevantes. 😊🥃🛒\n"
            "2. Si el usuario pregunta por productos, informa sobre ellos (precio, stock, variantes) usando la info del catálogo.\n"
            "3. Si un producto o variante no está disponible o sin stock según el catálogo, informa amablemente y sugiere alternativas del catálogo si aplica.\n"
            "4. Si el usuario muestra clara intención de comprar o añadir algo al pedido, guía la conversación para concretarlo:\n"
            "   - Confirma los productos y cantidades que quiere.\n"
            "   - Calcula el subtotal basado en precios del catálogo.\n"
            "   - Informa claramente que el envío tiene un costo fijo de COP 5.000 y calcula el total (Subtotal + 5000).\n"
            "   - Pregunta si desea agregar algo más o si eso es todo.\n"
            "   - **Si el usuario indica que ya terminó de añadir productos**, pídele los datos necesarios para el envío y procesamiento del pedido: nombre completo, dirección detallada (incluyendo ciudad/barrio si es posible), número de teléfono de contacto.\n"
            "   - Pregunta también su método de pago preferido (ej: 'contraentrega', 'transferencia Nequi/DaviPlata').\n"
            "5. NO pidas los datos de envío/pago hasta que el usuario indique que su lista de productos está completa.\n"
            "6. Cuando respondas, sé conversacional. **Al final de tu respuesta, si has logrado obtener al menos algunos datos del pedido (productos, o datos de envío, o método de pago), incluye un bloque JSON con la información estructurada que has podido extraer.** Incluso si el pedido no está completo (falta dirección, etc.), incluye el JSON con los datos que sí tienes. Esto ayuda a mi sistema a rastrear el progreso del pedido.\n"
            "   Formato del JSON (siempre incluir si hay datos de pedido, incluso si faltan campos):\n"
            "   ```json\n" # Indicar a Gemini que no ponga "json" antes de las llaves
            "   {\"order_details\":{\"name\":\"NOMBRE_OBTENIDO_O_NULL\",\"address\":\"DIRECCION_OBTENIDA_O_NULL\",\"phone\":\"TELEFONO_OBTENIDO_O_NULL\",\"payment_method\":\"METODO_PAGO_OBTENIDO_O_NULL\",\"products\":[{\"name\":\"NOMBRE_PRODUCTO_1\",\"quantity\":CANTIDAD_1,\"price\":PRECIO_UNITARIO_1}, {\"name\":\"NOMBRE_PRODUCTO_2\",\"quantity\":CANTIDAD_2,\"price\":PRECIO_UNITARIO_2}]}}\n" # Nota: Eliminamos 'total' del JSON de entrada para que process_order lo calcule internamente si es necesario.
            "   ```\n"
            "   - Si no hay productos identificados ni datos de envío/pago, no incluyas el bloque JSON.\n"
            "7. Si el usuario solo está haciendo preguntas generales o conversando, responde naturalmente sin forzar la venta o el JSON.\n"
            "8. Si el usuario pregunta específicamente por imágenes, y no se manejó en el paso anterior, puedes reiterar que puedes enviar fotos e invitarlo a especificar.\n"
            "9. Mantén las respuestas concisas pero claras."
        )

        # Usar un historial más amplio para el contexto general/pedido
        hist_for_general_llm = [m for m in user_histories[from_number] if m["role"] in ("user", "model")][-15:] # Últimos 15 turnos

        llm_input_general = hist_for_general_llm + [{"role": "user", "text": instrucciones_gemini}]

        # print(f"🧠 Enviando a Gemini para respuesta general/pedido...") # Opcional: log detallado
        llm_response_text_general = await ask_gemini_with_history(llm_input_general)
        # print(f"🧠 Respuesta de Gemini (general/pedido): {llm_response_text_general}") # Opcional: log detallado

        # 5. Extraer datos de pedido y texto de respuesta del LLM
        order_data, clean_text_response = extract_order_data(llm_response_text_general)
        print(f"📦 Datos de pedido extraídos por extractor: {order_data}")


        # 6. Guardar respuesta del modelo en historial y Supabase (el texto que se enviará)
        if clean_text_response and clean_text_response.strip():
            user_histories[from_number].append({
                "role": "model",
                "text": clean_text_response,
                "time": datetime.utcnow().isoformat()
            })
            try:
                await save_message_to_supabase(from_number, "model", clean_text_response)
                print("💾 Mensaje del modelo guardado en Supabase.")
            except Exception as e:
                print(f"❌ Error guardando mensaje del modelo en Supabase: {e}")
                # Continuar aunque falle la BD

            # 7. Enviar el texto de respuesta al usuario
            try:
                await send_whatsapp_message(from_number, clean_text_response)
                print("✅ Mensaje de WhatsApp enviado al usuario.")
            except Exception as e:
                print(f"❌ Error enviando mensaje de WhatsApp: {e}")
                # Considerar un mecanismo de reintento o notificación


        # 8. Procesar los datos del pedido si se extrajeron
        # Aquí usamos la lógica del primer código, llamando a process_order si hay algún order_data
        # (incluso si está incompleto) para que process_order maneje el estado.
        if order_data is not None and order_data != {}: # Verifica si extract_order_data devolvió algo
            print(f"⏳ Procesando pedido con datos extraídos: {order_data}")
            try:
                result_order = await process_order(from_number, order_data)
                status = result_order.get("status")
                print(f"🔄 Estado de process_order: {status}")

                # Enviar mensajes al usuario basados en el estado de process_order
                # Nota: Podrías evitar enviar estos mensajes si el clean_text_response del LLM
                # ya cubre adecuadamente el estado (ej: "Faltan datos..."). Depende de
                # qué tan bien controlas la respuesta del LLM vs la lógica del backend.
                # Aquí los enviamos después para asegurar que el usuario reciba la actualización
                # del estado del pedido según el backend.

                if status == "missing":
                    campos_faltantes = result_order.get("fields", [])
                    if campos_faltantes:
                         campos_texto = "\n".join(f"- {f.replace('_',' ').capitalize()}" for f in campos_faltantes)
                         # Solo enviar si el clean_text no menciona ya explícitamente los campos
                         # Podrías añadir una bandera al extractor o analizar clean_text
                         # Por ahora, asumimos que process_order es la fuente autorizada del estado
                         await send_whatsapp_message(from_number, f"📋 Para completar tu pedido, aún necesitamos algunos datos:\n{campos_texto}\n¿Podrías proporcionarlos, por favor?")
                    else:
                         # Caso raro: status missing pero sin campos. Solo logear.
                         print("⚠️ process_order devolvió 'missing' pero sin campos faltantes.")

                elif status == "created":
                    await send_whatsapp_message(from_number, "✅ ¡Tu pedido ha sido confirmado y creado con éxito! Muchas gracias por tu compra. 🎉 En breve te contactaremos sobre el envío.")
                    # Lógica de recomendaciones después de la creación
                    try:
                        recommended_prods = await get_recommended_products(order_data.get("products", []))
                        if recommended_prods:
                            texto_recomendaciones = "\n".join(f"- {r.get('name', 'Producto')}: COP {r.get('price', 'N/A')}" for r in recommended_prods)
                            await send_whatsapp_message(
                                from_number,
                                f"✨ ¡Excelente elección! Para complementar tu pedido, también te podrían interesar:\n{texto_recomendaciones}\n¿Te gustaría añadir alguno o tienes alguna otra pregunta?"
                            )
                    except Exception as e:
                        print(f"❌ Error obteniendo o enviando recomendaciones: {e}")

                elif status == "updated":
                    await send_whatsapp_message(from_number, "♻️ Tu pedido ha sido actualizado correctamente con la nueva información.")

                elif status == "no_change":
                    print("ℹ️ process_order indicó que no hubo cambios relevantes en el pedido.")
                    # No se envía mensaje adicional, la respuesta del LLM ya fue enviada.
                    pass
                else:
                    # Otro estado inesperado de process_order
                    print(f"⚠️ Estado inesperado o desconocido de process_order: {status}. Resultado completo: {result_order}")
                    # No enviar mensaje de error al usuario, a menos que se decida un fallback genérico.

            except Exception as e:
                 print(f"❌ Error al llamar a process_order o manejar su resultado: {e}")
                 traceback.print_exc()
                 # Considerar un mensaje de error al usuario si la operación de pedido falla críticamente
                 # await send_whatsapp_message(from_number, "Lo siento, tuve un problema al procesar los detalles de tu pedido. Por favor, intenta de nuevo.")

        else:
             # Si no se extrajeron datos de pedido, no hacemos nada más después de enviar el clean_text
             print("ℹ️ No se extrajeron datos de pedido relevantes en este turno.")


    except Exception:
        # Captura cualquier error no manejado en los pasos anteriores
        print("❌ [ERROR CRÍTICO en handle_user_message]:\n", traceback.format_exc())
        # Aquí puedes decidir si enviar un mensaje genérico de error al usuario
        # para que sepa que algo falló, pero sin exponer detalles internos.
        # try:
        #     await send_whatsapp_message(from_number, "Ups, parece que algo no salió bien de mi lado. Estoy teniendo problemas técnicos en este momento. Por favor, intenta de nuevo más tarde. Disculpa las molestias.")
        # except Exception as e:
        #     print(f"❌ ERROR ADICIONAL: Falló también el envío del mensaje de error genérico: {e}")