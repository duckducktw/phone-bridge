# Flexisip 2.6.1 proxy-only build with the lenient pn-prid patch.
# Built from source on an amd64 Debian trixie builder (see ../patches/*.patch),
# installed into /opt/belledonne-communications and packed as a tarball.
FROM debian:trixie-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl python3 python3-google-auth python3-requests \
       libc6 libcap2 libgcc-s1 libmbedcrypto16 libmbedtls21 libmbedx509-7 \
       libnghttp2-14 libssl3t64 libstdc++6 libsystemd0 libzstd1 zlib1g \
    && rm -rf /var/lib/apt/lists/*

ADD flexisip.tgz /

RUN mkdir -p /var/opt/belledonne-communications/log/flexisip /etc/flexisip \
    && ldconfig \
    && /opt/belledonne-communications/bin/flexisip --version

ENTRYPOINT ["/opt/belledonne-communications/bin/flexisip"]
CMD ["--server", "proxy"]
