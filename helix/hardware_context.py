"""Shared hardware context provider for Helix LLM workflows.

This module centralizes hardware context retrieval/formatting so both
`helix install` and `helix ask` use the same source of truth.
"""

from __future__ import annotations

import logging
from typing import Any

from helix.hardware_detection import detect_hardware

logger = logging.getLogger(__name__)


class HardwareContextProvider:
    """Builds structured and prompt-friendly hardware context."""

    @staticmethod
    def get_hardware_details(include_volatile: bool = True) -> dict[str, Any]:
        """Return normalized hardware details for LLM context.

        Args:
            include_volatile: Include rapidly changing fields (available RAM,
                free disk, network IP, uptime). Use False for cache-stable
                prompts.
        """
        info = detect_hardware()

        storage = []
        for disk in info.storage:
            item: dict[str, Any] = {
                "device": disk.device,
                "mount_point": disk.mount_point,
                "filesystem": disk.filesystem,
                "total_gb": round(disk.total_gb, 1),
                "used_gb": round(disk.used_gb, 1),
                "usage_percent": disk.usage_percent,
            }
            if include_volatile:
                item["available_gb"] = round(disk.available_gb, 1)
            storage.append(item)

        network = []
        for net in info.network:
            item: dict[str, Any] = {
                "interface": net.interface,
                "is_wireless": net.is_wireless,
                "speed_mbps": net.speed_mbps,
                "mac_address": net.mac_address,
                "vendor": net.vendor,
                "chipset": net.chipset,
                "pci_slot": net.pci_slot,
            }
            if include_volatile:
                item["ip_address"] = net.ip_address
            network.append(item)

        memory: dict[str, Any] = {
            "total_gb": info.memory.total_gb,
            "swap_total_mb": info.memory.swap_total_mb,
            "swap_free_mb": info.memory.swap_free_mb,
        }
        if include_volatile:
            memory["available_gb"] = info.memory.available_gb

        return {
            "system": {
                "hostname": info.hostname,
                "distro": info.distro,
                "distro_version": info.distro_version,
                "kernel_version": info.kernel_version,
                "virtualization": info.virtualization,
                "is_online": info.is_online,
                "is_vpn": info.is_vpn,
                "connection_quality": info.connection_quality,
                **({"uptime_seconds": info.uptime_seconds} if include_volatile else {}),
            },
            "cpu": {
                "vendor": info.cpu.vendor.value,
                "model": info.cpu.model,
                "architecture": info.cpu.architecture,
                "cores": info.cpu.cores,
                "threads": info.cpu.threads,
                "frequency_mhz": info.cpu.frequency_mhz,
                "features": info.cpu.features,
            },
            "gpu": [
                {
                    "vendor": g.vendor.value,
                    "model": g.model,
                    "memory_mb": g.memory_mb,
                    "driver_version": g.driver_version,
                    "cuda_version": g.cuda_version,
                    "compute_capability": g.compute_capability,
                    "pci_id": g.pci_id,
                }
                for g in info.gpu
            ],
            "memory": memory,
            "storage": storage,
            "network": network,
            "capabilities": {
                "has_nvidia_gpu": info.has_nvidia_gpu,
                "has_amd_gpu": info.has_amd_gpu,
                "has_intel_gpu": info.has_intel_gpu,
                "cuda_available": info.cuda_available,
                "rocm_available": info.rocm_available,
                "gpu_mode": info.gpu_mode,
                "is_hybrid_system": info.is_hybrid_system,
                "render_offload_available": info.render_offload_available,
            },
            "proxy": info.proxy,
        }

    @classmethod
    def to_prompt_text(cls, include_volatile: bool = False) -> str:
        """Format hardware details for concise system-prompt insertion."""
        try:
            hw = cls.get_hardware_details(include_volatile=include_volatile)
            lines: list[str] = []

            system = hw.get("system", {})
            cpu = hw.get("cpu", {})
            memory = hw.get("memory", {})
            storage = hw.get("storage", [])
            gpu = hw.get("gpu", [])
            caps = hw.get("capabilities", {})

            distro = " ".join(
                p for p in [system.get("distro", ""), system.get("distro_version", "")] if p
            ).strip()
            if distro:
                lines.append(f"OS: {distro}")
            if system.get("kernel_version"):
                lines.append(f"Kernel: {system['kernel_version']}")
            if cpu.get("architecture"):
                lines.append(f"Arch: {cpu['architecture']}")

            if cpu.get("model"):
                lines.append(
                    f"CPU: {cpu['model']} ({cpu.get('cores', 0)} cores, {cpu.get('threads', 0)} threads)"
                )
            if cpu.get("features"):
                lines.append(f"CPU features: {', '.join(cpu['features'])}")

            if gpu:
                gpu_items = []
                for g in gpu:
                    item = g.get("model") or "Unknown GPU"
                    if g.get("memory_mb"):
                        item += f" ({g['memory_mb']} MB)"
                    gpu_items.append(item)
                lines.append(f"GPU: {', '.join(gpu_items)}")
            else:
                lines.append("GPU: None detected")

            lines.append(f"CUDA available: {bool(caps.get('cuda_available'))}")
            lines.append(f"ROCm available: {bool(caps.get('rocm_available'))}")
            if caps.get("gpu_mode"):
                lines.append(f"GPU mode: {caps.get('gpu_mode')}")
            if caps.get("is_hybrid_system") is not None:
                lines.append(f"Hybrid GPU system: {bool(caps.get('is_hybrid_system'))}")

            lines.append(f"RAM: {memory.get('total_gb', 0)} GB")
            if include_volatile and memory.get("available_gb") is not None:
                lines.append(f"RAM available: {memory.get('available_gb')} GB")

            if storage:
                root = next((s for s in storage if s.get("mount_point") == "/"), storage[0])
                lines.append(f"Disk: {root.get('total_gb', 0)} GB total")
                if include_volatile and root.get("available_gb") is not None:
                    lines.append(f"Disk free: {root.get('available_gb', 0)} GB")

            if system.get("virtualization"):
                lines.append(f"Virtualization: {system['virtualization']}")
            if system.get("is_online") is not None:
                lines.append(f"Online: {bool(system.get('is_online'))}")
            if system.get("is_vpn") is not None:
                lines.append(f"VPN: {bool(system.get('is_vpn'))}")
            if system.get("connection_quality"):
                lines.append(f"Network quality: {system['connection_quality']}")

            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"Failed to build hardware prompt context: {e}")
            return ""
