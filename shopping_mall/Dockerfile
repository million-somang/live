FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALL_MODE=cloud \
    OBS_PULL_ENABLED=0 \
    PORT=3000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/static/uploads/products

EXPOSE 3000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:3000", "--workers", "1", "--threads", "16", "--timeout", "120"]
