FROM python:3.11-slim

# Create non-root user
RUN useradd --uid 1000 --create-home appuser

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/
COPY run.py .
COPY migrations/ ./migrations/
COPY entrypoint.sh .

# Create data and logs directories with correct ownership
RUN mkdir -p data logs && chown -R appuser:appuser /app

RUN chmod +x entrypoint.sh

ENV FLASK_APP=run.py
ENV FLASK_ENV=production

USER appuser

EXPOSE 5001

ENTRYPOINT ["./entrypoint.sh"]
