# 1. Use a slim Python image to keep the footprint small
FROM python:3.11-slim

# 2. Set the working directory
WORKDIR /app

# 3. Copy only the requirements file first to leverage Docker caching
COPY requirements.txt .

# 4. Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy only the specific application files
# These are the three core modules needed for the bot to run
COPY main.py bot.py dashboard.py ./

# 6. Expose the port for the Dash dashboard
EXPOSE 8050

# 7. Run the application
CMD ["python", "main.py"]