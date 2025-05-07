# app/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import webhook
from app.routes import products
from app.routes import orders    # 👈 Nuevo import

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Ajusta según el origen de tu frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook.router)
app.include_router(products.router)
app.include_router(orders.router)    # 👈 Registramos el router de órdenes

@app.get("/")
def root():
    return {"status": "ok"}
