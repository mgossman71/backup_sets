# Real-browser tests in Chromium, Firefox and WebKit (Safari's engine), via Playwright's
# official image (Ubuntu 22.04 with all three browsers and their system libraries).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy
COPY portal/requirements.txt /tmp/portal-requirements.txt
RUN pip install --no-cache-dir -r /tmp/portal-requirements.txt pytest==8.2.2 playwright==1.47.0
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
