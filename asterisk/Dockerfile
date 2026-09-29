# Asterisk 22 LTS built from source: chan_websocket (media over WebSocket with
# JSON control messages) needs 22.8+, newer than distro packages ship.
FROM debian:bookworm-slim AS build
ARG ASTERISK_VERSION=22.11.0
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates wget bzip2 patch python3 pkg-config \
      libedit-dev uuid-dev libjansson-dev libxml2-dev libsqlite3-dev libssl-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /usr/src
RUN wget -q https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-${ASTERISK_VERSION}.tar.gz \
    && tar xzf asterisk-${ASTERISK_VERSION}.tar.gz
WORKDIR /usr/src/asterisk-${ASTERISK_VERSION}
RUN ./configure --with-pjproject-bundled --without-dahdi --without-pri --without-ss7 >/dev/null \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE --enable chan_websocket --enable res_websocket_client menuselect.makeopts \
    && make -j"$(nproc)" >/dev/null \
    && make install DESTDIR=/out >/dev/null \
    && rm -rf /out/var/run  # a symlink to /run in the runtime image

FROM debian:bookworm-slim
ARG ASTERISK_VERSION=22.11.0
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates gettext-base libedit2 libuuid1 libjansson4 libxml2 libsqlite3-0 libssl3 libcurl4 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --home /var/lib/asterisk asterisk
COPY --from=build /out/ /
# Australian ringback/busy tones (heard during transfers).
COPY --from=build /usr/src/asterisk-${ASTERISK_VERSION}/configs/samples/indications.conf.sample /etc/asterisk/indications.conf
RUN sed -i 's/^country=.*/country=au/' /etc/asterisk/indications.conf \
    && cd /etc/asterisk && touch acl.conf ccss.conf cdr.conf cel.conf chan_websocket.conf features.conf \
       pjproject.conf pjsip_wizard.conf stasis.conf udptl.conf  # defaults are fine; silences startup errors
RUN ldconfig && mkdir -p /var/run/asterisk /var/log/asterisk /var/spool/asterisk /var/lib/asterisk \
    && chown -R asterisk:asterisk /var/run/asterisk /var/log/asterisk /var/spool/asterisk /var/lib/asterisk /etc/asterisk
COPY config/ /etc/asterisk/templates/
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
