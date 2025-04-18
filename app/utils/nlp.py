from app.clients.gemini import ask_gemini

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
        "todo el catálogo",
        "qué vendes",
        "qué hay disponible",
        "ver catálogo",
        "ver todos los productos",
        "mostrar todo"
    ]
    texto = texto.lower()
    return any(frase in texto for frase in frases)


async def detecta_pedido_de_productos(texto_usuario: str) -> bool:
    """
    Usa IA para detectar si el usuario quiere ver todos los productos, si las frases no lo indican claramente.
    """
    if quiere_ver_todos_los_productos(texto_usuario):
        return True

    prompt = (
        f"¿El siguiente mensaje indica que el usuario quiere ver todos los productos disponibles en una tienda? "
        f"Responde solo con 'sí' o 'no':\n\n{texto_usuario}"
    )
    respuesta = await ask_gemini(prompt)
    return "sí" in respuesta.lower()
