FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (ffmpeg is needed for voice recognition)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Expose port
EXPOSE 8080

# Run the app
CMD ["python", "main.py"]
