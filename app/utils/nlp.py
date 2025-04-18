from app.clients.gemini import ask_gemini_with_history

def extract_keywords(text: str, keywords: list[str]) -> list[str]:
    """
    Extrae palabras clave definidas manualmente si aparecen en el texto.
    """
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


def quiere_ver_todos_los_productos(texto: str) -> bool:
    """
    Detecta si el mensaje contiene frases comunes que indican que el usuario quiere ver todos los productos.
    """
    frases = [
        "muéstrame todos los productos",
        "quiero ver todos los productos",
        "enséñame los productos",
        "qué productos tienes",
        "que productos tienes",
        "todo el catálogo",
        "qué vendes",
        "que vendes",
        "qué hay disponible",
        "ver catálogo",
        "ver todos los productos",
        "mostrar todo"
    ]
    texto = texto.lower()
    return any(frase in texto for frase in frases)


async def detecta_pedido_de_productos(texto_usuario: str) -> bool:
    """
    Usando IA, detecta si el usuario está pidiendo ver todos los productos
    (solo si las frases comunes no fueron suficientes).
    """
    # 1) Si coincide con alguna frase común, devolvemos True
    if quiere_ver_todos_los_productos(texto_usuario):
        return True

    # 2) Si no, preguntamos a Gemini con un prompt simple
    prompt = (
        "¿El siguiente mensaje indica que el usuario quiere ver todos "
        "los productos disponibles en una tienda? Responde solo con 'sí' o 'no':\n\n"
        f"{texto_usuario}"
    )

    # Para llamar a Gemini, usamos la misma función que tu flujo principal
    # pero pasando un historial mínimo con un solo mensaje de usuario.
    respuesta = await ask_gemini_with_history([{"role": "user", "text": prompt}])
    return "sí" in respuesta.lower()
