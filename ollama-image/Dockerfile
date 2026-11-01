# compbot's bundled Ollama: the official image plus what NVIDIA's Vulkan driver needs inside a container.
#
# Why: Ollama's CUDA build refuses older NVIDIA cards (GTX 9xx / 10xx, compute 5.0-6.2) unless the driver is 570+,
# and TrueNAS ships 550. Through Vulkan those cards still run models on the GPU. Newer cards and drivers are
# unaffected: when CUDA and Vulkan both see a GPU, Ollama uses CUDA.
#
# Published as ghcr.io/<owner>/compbot:ollama-latest (and :ollama-<version>) by .github/workflows/ollama-image.yml,
# which rebuilds daily so it follows new Ollama releases.
ARG OLLAMA_TAG=latest
FROM ollama/ollama:${OLLAMA_TAG}

# libGLX_nvidia.so.0 is NVIDIA's Vulkan driver. The NVIDIA container toolkit mounts it from the host when
# NVIDIA_DRIVER_CAPABILITIES includes "graphics"; it needs glvnd and a few X libraries from the image.
# vulkan-tools adds vulkaninfo for troubleshooting: docker exec compbot-ollama vulkaninfo --summary
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglvnd0 libegl1 libx11-6 libxext6 vulkan-tools \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY nvidia_icd.json /usr/local/share/compbot/nvidia_icd.json
COPY entrypoint.sh /usr/local/bin/compbot-ollama-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/compbot-ollama-entrypoint && chmod 755 /usr/local/bin/compbot-ollama-entrypoint

ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics
ENTRYPOINT ["/usr/local/bin/compbot-ollama-entrypoint"]
CMD ["serve"]
