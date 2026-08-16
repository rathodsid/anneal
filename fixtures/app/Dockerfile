# The M0 hermetic golden app. Chromium-only target; no external network at runtime.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
EXPOSE 8099
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8099"]
