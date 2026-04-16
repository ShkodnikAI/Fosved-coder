FROM python:3.11-slim

WORKDIR /app

# Install git (needed for ideas_injector and git operations)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directory for SQLite
RUN mkdir -p /app/data

RUN chmod +x start.sh

EXPOSE 8000

ENTRYPOINT ["./start.sh"]
