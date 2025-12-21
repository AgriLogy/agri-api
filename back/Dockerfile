FROM python:3.12.8

ENV PYTHONUNBUFFERED=1
WORKDIR /code

# Install system packages
RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

# Copy and install Python packages
COPY requirements.txt /code/
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Copy project files
COPY . /code/

# Entrypoint script
COPY run.sh /run.sh
COPY env-example /.env
RUN chmod +x /run.sh

RUN mkdir -m 777 /shared


CMD ["bash", "/run.sh"]
