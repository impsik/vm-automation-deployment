FROM vmware/govc:latest AS govc
FROM python:3.13-alpine

WORKDIR /app
COPY --from=govc /govc /usr/local/bin/govc

RUN apk add --no-cache gcc musl-dev libffi-dev openssl-dev libvirt-client qemu-img xorriso \
    && pip install --no-cache-dir ansible-core pyvmomi ldap3 \
    && ansible-galaxy collection install community.vmware \
    && apk del gcc musl-dev libffi-dev openssl-dev \
    && addgroup -g 1000 portal && adduser -D -u 1000 -G portal portal \
    && mkdir -p /data /var/run/libvirt \
    && chown portal:portal /data

COPY portal/app.py ./
COPY portal/static ./static
COPY portal/entrypoint.sh /entrypoint.sh
COPY ansible ./ansible
COPY tests ./tests

RUN chmod 0755 /entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "app.py"]
