from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True}

@app.get("/debug/env")
def debug_env():
    return {
        "SUPABASE_URL": bool(os.getenv("SUPABASE_URL")),
        "SUPABASE_SERVICE_ROLE_KEY": bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
    }
