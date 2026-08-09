FROM python:3.11-slim

LABEL org.opencontainers.image.description="Prometheus exporter for TP-Link Kasa smart plugs and power strips that exposes real-time power consumption, voltage, current, and energy metrics"

WORKDIR /app

# Install dependencies including tzdata for timezone resolution
RUN pip install --no-cache-dir "python-kasa[speedups]" prometheus_client pyyaml tzdata

COPY kasa-exporter.py exporter.py

EXPOSE 9233

CMD ["python", "exporter.py"]