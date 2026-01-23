# ប្រើ Python 3.10 (Slim version ដើម្បីឱ្យស្រាល)
FROM python:3.10-slim

# កំណត់ Environment Variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ដំឡើង System Dependencies (FFmpeg សំខាន់បំផុត!)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# កំណត់ Folder ការងារ
WORKDIR /app

# Copy និង Install Python Libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy កូដទាំងអស់ចូលក្នុង Image
COPY . .

# បញ្ជាឱ្យ Run Bot
CMD ["sh", "-c", "pip install -U yt-dlp && python main.py"]