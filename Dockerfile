FROM python:3.11-slim

WORKDIR /app

# Install python-kasa with speedups for optimized JSON parsing
RUN pip install --no-cache-dir "python-kasa[speedups]" prometheus_client pyyaml

COPY kasa-exporter.py exporter.py

EXPOSE 9233

CMD ["python", "exporter.py"]
