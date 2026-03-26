# Poker Backend Dockerfile
# Optimized for Cloud Run deployment

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Cloud Run uses PORT environment variable
ENV PORT=8080
ENV HOST=0.0.0.0

# Expose port (informational)
EXPOSE 8080

# Run with uvicorn
# --host 0.0.0.0 required for container networking
# --port uses PORT env var for Cloud Run compatibility
CMD ["python", "-m", "uvicorn", "src.server.app:app", "--host", "0.0.0.0", "--port", "8080"]
