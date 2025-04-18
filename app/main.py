from fastapi import FastAPI
from app.routes import webhook
from app.routes import products



app = FastAPI()

app.include_router(webhook.router)
app.include_router(products.router)


@app.get("/")
def root():
    return {"status": "ok"}

