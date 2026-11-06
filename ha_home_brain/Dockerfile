ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /opt/home_brain

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY rootfs /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
