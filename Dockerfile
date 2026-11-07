FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .
ENV PYTHONUNBUFFERED=1
ENV SHOW_BROWSER=false
ENV CSV_PATH=/data/results.csv

CMD ["python", "scraper.py"]
