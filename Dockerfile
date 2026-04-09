# Stage 1: Build the frontend
FROM node:18-alpine AS frontend-builder
WORKDIR /app/frontend
# Install dependencies
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
# Copy frontend code and build
COPY frontend/ ./
RUN npm run build

# Stage 2: Build the backend and combine
FROM python:3.11-slim
WORKDIR /app

# Install system dependencies required for GIS operations
RUN apt-get update && apt-get install -y \
    build-essential \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./backend/
COPY main.py ./

# Copy frontend build from stage 1
COPY --from=frontend-builder /app/frontend/build ./frontend/build

# Expose the API port
EXPOSE 8000

# Start FastAPI server
CMD ["python", "-m", "backend.main"]
