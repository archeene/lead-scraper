FROM pyd4vinci/scrapling:latest

WORKDIR /app

# Copy everything needed for install
COPY pyproject.toml .
COPY app/ app/

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
