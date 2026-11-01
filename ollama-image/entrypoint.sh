#!/bin/sh
# Start Ollama, first pointing the Vulkan loader at NVIDIA's driver when the NVIDIA container toolkit mounted the
# driver's libraries but not its Vulkan manifest (the official Ollama image has no manifest of its own).
icd=/usr/local/share/compbot/nvidia_icd.json

has_glx_nvidia() {
  ldconfig -p 2>/dev/null | grep -q 'libGLX_nvidia\.so\.0' && return 0
  for d in /usr/lib/x86_64-linux-gnu /usr/lib64 /usr/lib /usr/local/nvidia/lib64 /usr/local/nvidia/lib; do
    [ -e "$d/libGLX_nvidia.so.0" ] && return 0
  done
  return 1
}

if [ -z "${VK_DRIVER_FILES}${VK_ICD_FILENAMES}" ]; then
  if ls /etc/vulkan/icd.d/nvidia_icd*.json /usr/share/vulkan/icd.d/nvidia_icd*.json >/dev/null 2>&1; then
    echo "compbot-ollama: using the NVIDIA Vulkan manifest mounted by the container toolkit" >&2
  elif has_glx_nvidia; then
    export VK_DRIVER_FILES="$icd" VK_ICD_FILENAMES="$icd"
    echo "compbot-ollama: NVIDIA Vulkan driver found; Vulkan enabled for older NVIDIA cards" >&2
  elif [ -e /dev/nvidiactl ]; then
    echo "compbot-ollama: an NVIDIA GPU is attached but not its graphics libraries, so Vulkan can't use it." \
         "Set NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics on this container." >&2
  fi
fi

exec /bin/ollama "$@"
