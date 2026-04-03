# BV Benchmark Business Case — Docker image
#
# Build:  docker build -t bv-bizcase .
# Run:    docker run -p 8501:8501 bv-bizcase
# Or use: docker compose up

FROM python:3.11-slim

# System deps needed by openpyxl/oletools and plotly
RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8501

# Health-check so orchestrators know when the app is ready
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "app/main.py", \
            "--server.port=8501", \
            "--server.address=0.0.0.0", \
            "--server.headless=true"]
