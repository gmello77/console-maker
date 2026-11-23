FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends parted fdisk e2fsprogs util-linux rsync squashfs-tools dosfstools \
 && rm -rf /var/lib/apt/lists/*
COPY build_image.sh extract_defaults.sh /
RUN chmod +x /build_image.sh /extract_defaults.sh
