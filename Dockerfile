FROM python:3.10-slim

# Set environment variable to inform the application of the running port
ENV PORT 8080

# Install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application's code into the container
COPY . .

# Command to run the twilio.py script
CMD ["python", "twilio.py"]

# Expose the port the app runs on
EXPOSE 8080
