# PCLP2 x86_64 toolchain.
# The host is arm64, but the exam VM is x86_64 and the templates are 64-bit (elf64).
# This image gives us a real x86_64 NASM + GCC + objconv toolchain.
FROM --platform=linux/amd64 debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        nasm \
        gcc \
        g++ \
        make \
        binutils \
        libc6-dev \
        gdb \
        file \
        curl \
        unzip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# objconv (Agner Fog) turns a compiled .o back into clean NASM source.
# This is what powers `c2asm`: C -> object -> NASM.
RUN set -eux; \
    cd /tmp; \
    curl -fsSL -o objconv.zip https://www.agner.org/optimize/objconv.zip; \
    unzip -q objconv.zip; \
    unzip -q -o source.zip -d objconv_src; \
    cd objconv_src; \
    g++ -O2 -o /usr/local/bin/objconv *.cpp; \
    cd /; rm -rf /tmp/*; \
    objconv 2>&1 | head -1 || true

WORKDIR /work
