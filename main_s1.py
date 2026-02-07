import os
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
TV_TOKEN = os.getenv("TV_TOKEN", "")

print("TV_TOKEN set?", bool(TV_TOKEN), "len=", len(TV_TOKEN))

last_payload = None

@app.get("/")
def root():
    return {"ok": True, "service": "tv-webhook-receiver"}

@app.get("/last")
def last():
    return {"ok": True, "last": last_payload}

async def handle_webhook(req: Request):
    global last_payload
    data = await req.json()

    token = str(data.get("token", ""))
    if TV_TOKEN and token != TV_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    # не логируй токен целиком
    safe = dict(data)
    if "token" in safe:
        safe["token"] = "***"
    print("WEBHOOK:", safe)

    last_payload = data
    return {"ok": True}

@app.post("/webhook")
async def webhook(req: Request):
    return await handle_webhook(req)

@app.post("/")
async def webhook_root(req: Request):
    return await handle_webhook(req)
