import os
import uvicorn

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


def run():
    host = os.environ["APP_HOST"]
    port = int(os.environ["APP_PORT"])
    uvicorn.run(app, host=host, port=port)
