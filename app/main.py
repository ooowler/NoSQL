import os
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


def run():
    port = int(os.environ.get("APP_PORT", "8080"))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
