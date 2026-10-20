FROM alpine:3.21

RUN apk add --no-cache dnsmasq inotify-tools

COPY --chmod=755 watch-dhcp-hosts /usr/local/bin/watch-dhcp-hosts

# Config and dhcp-hosts are mounted at runtime (see compose.build.yaml).
EXPOSE 53/udp 67/udp

ENTRYPOINT ["watch-dhcp-hosts"]
CMD ["dnsmasq", "--log-facility=-", "--conf-file=/var/lib/misc/dnsmasq-active.conf"]
