# Use the official Python 3.9 base image
FROM python:3.13

# Set the working directory in the container
WORKDIR /app

# Copy the requirements.txt file into the container
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot script into the container
COPY tele-bot.py .

# Expose any ports the bot might use (e.g., if using webhooks)
EXPOSE 8443

# Set the tele-bot.py script as the default command
CMD ["python", "tele-bot.py"]