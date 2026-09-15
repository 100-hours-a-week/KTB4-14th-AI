FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY ai_service ./ai_service
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "ai_service.main:app", "--host", "0.0.0.0", "--port", "8000"]

