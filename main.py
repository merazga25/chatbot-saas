from fastapi import FastAPI
from supabase import create_client
import os

app = FastAPI()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.get("/")
def root():
    return {"ok": True}

@app.get("/debug/supabase")
def debug_supabase():
    res = supabase.table("shops").select("id, name").limit(1).execute()
    return {
        "ok": True,
        "data": res.data
    }
