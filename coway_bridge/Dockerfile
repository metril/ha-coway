FROM python:3.12-alpine

# Legacy TLS 1.0 / SECLEVEL=0 is required by the purifier firmware; it is enabled per
# SSLContext in code, but make sure the system OpenSSL config does not forbid it.
RUN apk add --no-cache openssl libffi \
    && printf '%s\n' \
       'openssl_conf = openssl_init' \
       '[openssl_init]' \
       'ssl_conf = ssl_sect' \
       '[ssl_sect]' \
       'system_default = system_default_sect' \
       '[system_default_sect]' \
       'MinProtocol = TLSv1' \
       'CipherString = DEFAULT:@SECLEVEL=0' > /etc/ssl/openssl-coway.cnf

ENV OPENSSL_CONF=/etc/ssl/openssl-coway.cnf \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/coway
# Build context is this directory (coway_bridge/); run directly from source, deps from wheels.
RUN pip install "aiohttp>=3.9" "aiomqtt>=2.0" "cryptography>=41"
ENV PYTHONPATH=/opt/coway/app
COPY app/coway_bridge /opt/coway/app/coway_bridge

COPY run.sh /run.sh
RUN chmod +x /run.sh
EXPOSE 9090 8099
CMD ["/run.sh"]
