        if action.get("want_images"):
            # 5.4) Si Gemini indica want_images, procesar
            target_raw = action.get("target", "")
            target = target_raw.strip().lower()
            print(f"🔍 [DEBUG] Gemini target_raw: '{target_raw}' → normalized target: '{target}'")

            # Fallback último context
            if not target:
                for e in reversed(user_histories[from_number]):
                    if e.get("role") == "context":
                        target = e["last_image_selection"]["product_name"].lower()
                        print(f"🔍 [DEBUG] Fallback context target: '{target}'")
                        break

            # Mostrar todas las claves disponibles
            print(f"🔍 [DEBUG] choice_map keys ({len(choice_map)}): {list(choice_map.keys())[:10]}{'…' if len(choice_map)>10 else ''}")

            # Match insensible a mayúsculas
            candidates = list(choice_map.keys())
            match = get_close_matches(target, candidates, n=1, cutoff=0.4)
            print(f"🔍 [DEBUG] get_close_matches('{target}', …) → {match}")

            if match:
                prod, var = choice_map[match[0]]
                print(f"🔍 [DEBUG] Matched to product '{prod['name']}', variant: {var}")

                # … (tu guardado de contexto) …

                # Recopilar URLs
                if var and var.get("product_images"):
                    urls = [img["url"] for img in var["product_images"] 
                            if img["url"].lower().endswith((".png",".jpg",".jpeg"))]
                else:
                    imgs = prod.get("product_images", [])
                    urls = [imgs[0]["url"]] if imgs and imgs[0]["url"].lower().endswith((".png",".jpg",".jpeg")) else []

                print(f"🔍 [DEBUG] URLs seleccionadas para envío: {urls}")

                # Envío…