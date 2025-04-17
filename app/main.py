from fastapi import FastAPI
from app.routes import webhook

app = FastAPI()

# Incluye el router del webhook (maneja GET y POST en /webhook)
app.include_router(webhook.router)

# Ruta raíz para verificar que el backend está corriendo
@app.get("/")
def root():
    return {"status": "ok"}
