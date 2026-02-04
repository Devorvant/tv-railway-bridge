import os
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

TV_TOKEN = os.getenv("TV_TOKEN", "")

@app.get("/")
def root():
    return {"ok": True, "service": "tv-webhook-receiver"}

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()

    token = str(data.get("token", ""))
    if TV_TOKEN and token != TV_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    print("WEBHOOK:", data)
    return {"ok": True}
