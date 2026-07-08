from fastapi import FastAPI

app = FastAPI(title="Almighty PR Review Server")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
