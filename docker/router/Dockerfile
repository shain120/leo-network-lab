FROM ubuntu:22.04

LABEL org.leo-network-lab.project="leo-network-lab"
LABEL org.leo-network-lab.role="router"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 \
    iputils-ping \
    net-tools \
    iperf3 \
    curl \
    wget \
    traceroute \
    tcpdump \
    ethtool \
    dnsutils \
    iptables \
    python3 \
    python3-pip \
    procps \
    jq \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

CMD ["/bin/bash"]
