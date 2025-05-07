from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware  # 👉 Añadido para CORS
from app.routes import webhook
from app.routes import products

app = FastAPI()

# 👇 Middleware para permitir peticiones desde tu frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # o "*" para cualquier origen
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook.router)
app.include_router(products.router)

@app.get("/")
def root():
    return {"status": "ok"}
