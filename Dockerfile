FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium

# Copy application code
COPY scripts/ scripts/

# Create directories for local fallback (S3 replaces these in Phase 3)
RUN mkdir -p logs data

# Default: run the Streamlit web UI
EXPOSE 8501
CMD ["python3", "-m", "streamlit", "run", "scripts/app.py", "--server.address", "0.0.0.0", "--server.port", "8501"]
