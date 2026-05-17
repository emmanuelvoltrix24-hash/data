FROM python:3.12-slim

# Install system dependencies required by Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core system libraries
    libglib2.0-0 \
    libdbus-1-3 \
    libexpat1 \
    # X11 / display libraries
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxi6 \
    libxrender1 \
    libxtst6 \
    libxcursor1 \
    # Graphics / GPU
    libcairo2 \
    libdrm2 \
    libgbm1 \
    # Fonts / text rendering
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libharfbuzz0b \
    libfontconfig1 \
    libfreetype6 \
    # Accessibility / ATK
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    # NSS / crypto (required by Chromium)
    libnss3 \
    libnssutil3 \
    libsmime3 \
    libnspr4 \
    # Printing / CUPS
    libcups2 \
    # Audio
    libasound2 \
    # xkbcommon (keyboard handling)
    libxkbcommon0 \
    # Virtual display for headless operation
    xvfb \
    # Utilities needed at runtime
    ca-certificates \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's bundled Chromium browser
RUN python -m playwright install chromium

# Copy application source
COPY . .

# Expose the default Railway port
EXPOSE 8080

# Run the learner first (one-shot), then start the main process
CMD python run_learner.py && python master.py
