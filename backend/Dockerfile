FROM python:3.12

WORKDIR /app

RUN apt-get update && \
    apt-get install -y git nginx && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/projects

COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
EXPOSE 8000

CMD service nginx start && uvicorn main:app --host 0.0.0.0 --port 8000
