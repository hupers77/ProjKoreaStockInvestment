FROM python:3.12-slim
ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME /app/data
EXPOSE 5000
CMD ["python", "run.py", "--host", "0.0.0.0", "--no-browser"]
