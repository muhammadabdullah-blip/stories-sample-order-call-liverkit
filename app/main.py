from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.calls import router as calls_router
from app.routes.webhooks import router as webhooks_router

app = FastAPI(
    title="LiveKit Outbound Call API",
    description=(
        "REST API for initiating and managing outbound AI voice calls. "
        "Built on LiveKit + Twilio SIP with Supabase for persistence."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(calls_router)
app.include_router(webhooks_router)


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}
