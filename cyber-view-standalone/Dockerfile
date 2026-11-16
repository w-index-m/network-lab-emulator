FROM python:3.11-slim
WORKDIR /app
# 社内プロキシ等の独自 CA(extra-ca/*.crt)があれば信頼する。無ければ何もしない
COPY extra-ca/ /tmp/extra-ca/
RUN if ls /tmp/extra-ca/*.crt >/dev/null 2>&1; then \
      cp /tmp/extra-ca/*.crt /usr/local/share/ca-certificates/ && update-ca-certificates; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py ./
COPY cvs ./cvs
COPY web ./web
EXPOSE 8090
CMD ["python", "server.py", "-c", "/config/config.yaml", "--port", "8090"]
