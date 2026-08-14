# WiFile web UI — pure Python stdlib, no build step.
FROM python:3.11-slim

WORKDIR /app

COPY wifile.py webui.py ./
COPY wifile_web/ ./wifile_web/

# 8765 is the web UI; 12345 is the default transfer port the sender listens
# on. Publish both when running the container.
EXPOSE 8765 12345

# --host 0.0.0.0 is required so Docker's published port can reach the UI.
# Keep a fixed port: the automatic free-port fallback would pick a port
# that is not published.
CMD ["python", "webui.py", "--host", "0.0.0.0", "--port", "8765"]
