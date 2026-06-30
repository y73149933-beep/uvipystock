# Worker Dockerfile: Matching worker (consumes queue:orders)
# Same image as backend, different entrypoint

# Reuse the backend image (already has Cython compiled + deps installed)
FROM crypto-exchange-backend:latest AS worker

# The backend image's WORKDIR is /app (where alembic.ini lives).
# The worker needs to run from /app/backend/ so that `app.matching.worker`
# resolves correctly. PYTHONPATH is already set to /app by the base image.
WORKDIR /app/backend

# Override CMD to run the matching worker instead of uvicorn
CMD ["python", "-m", "app.matching.worker"]
