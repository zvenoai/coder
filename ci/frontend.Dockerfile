FROM node:22-slim

WORKDIR /app

# Install deps first (cached layer when package files unchanged)
COPY package.json package-lock.json ./
RUN npm ci

# Copy source code
COPY . .
