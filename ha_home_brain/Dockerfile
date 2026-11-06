ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Dependintele Python se adauga aici cand scriem codul:
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt

COPY rootfs /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
