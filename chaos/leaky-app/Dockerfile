FROM python:3.12-slim

# The app deliberately misbehaves on request; nothing here should ever run
# outside the chaos compose stack.
LABEL org.opencontainers.image.description="SysHealth chaos target - intentionally faulty"

RUN pip install --no-cache-dir flask>=3.0

WORKDIR /app
COPY app.py .

EXPOSE 8080
CMD ["python", "app.py"]
