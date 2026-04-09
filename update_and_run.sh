#!/bin/bash

echo "[+] Pulling latest code..."
git pull

echo "[+] Creating required directories if they don't exist..."
mkdir -p cache .garmin_cache
if [ ! -f .env ]; then
    echo "[!] .env file not found! Copying .env.example to .env."
    echo "    Please make sure to add your Garmin credentials to .env!"
    cp .env.example .env
fi

echo "[+] Building the new Docker image..."
docker build -t garmin-city-explorer .

echo "[+] Stopping old container if it exists..."
docker stop garmin-app 2>/dev/null
docker rm garmin-app 2>/dev/null

echo "[+] Starting new container..."
docker run -d -p 8000:8000 \
  -v "$PWD/cache:/app/cache" \
  -v "$PWD/.garmin_cache:/app/.garmin_cache" \
  -v "$PWD/.env:/app/.env" \
  --name garmin-app garmin-city-explorer

echo "[+] Success! The updated app is running in the background."
