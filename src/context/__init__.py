"""Paquete N7: Context Awareness Engine para observación segura en Windows."""

from __future__ import annotations

from src.context.console import ContextConsoleHandler
from src.context.engine import ContextAwarenessEngine
from src.context.filesystem_provider import FileSystemContextProvider, classify_file_category
from src.context.models import (
    ALL_CONTEXT_CATEGORIES,
    ActiveWindowInfo,
    ApplicationAwarenessInfo,
    ApplicationItem,
    AvailabilityStatus,
    ContextAccessDeniedError,
    ContextCategory,
    ContextError,
    ContextPolicyError,
    ContextProviderError,
    ContextSnapshot,
    ContextTimeoutError,
    ContextUntrustedEnvelope,
    DirectorySnapshot,
    FileMetadataItem,
    FileSystemContextInfo,
    GpuDeviceInfo,
    HardwareContextInfo,
    MetricReading,
    ProcessAwarenessInfo,
    ProcessContextItem,
    ProviderIssue,
    ResourceUsageInfo,
    StorageVolumeInfo,
    SystemContextInfo,
)
from src.context.policy import (
    ContextAccessPolicy,
    is_sensitive_context_filename,
    sanitize_untrusted_label,
)
from src.context.process_app_provider import (
    ApplicationAwarenessProvider,
    ProcessAwarenessProvider,
)
from src.context.system_hardware_provider import (
    HardwareInfoProvider,
    ResourceMonitoringProvider,
    StorageInfoProvider,
    SystemInfoProvider,
)

__all__ = [
    "ALL_CONTEXT_CATEGORIES",
    "ActiveWindowInfo",
    "ApplicationAwarenessInfo",
    "ApplicationAwarenessProvider",
    "ApplicationItem",
    "AvailabilityStatus",
    "ContextAccessDeniedError",
    "ContextAccessPolicy",
    "ContextAwarenessEngine",
    "ContextCategory",
    "ContextConsoleHandler",
    "ContextError",
    "ContextPolicyError",
    "ContextProviderError",
    "ContextSnapshot",
    "ContextTimeoutError",
    "ContextUntrustedEnvelope",
    "DirectorySnapshot",
    "FileMetadataItem",
    "FileSystemContextInfo",
    "FileSystemContextProvider",
    "GpuDeviceInfo",
    "HardwareContextInfo",
    "HardwareInfoProvider",
    "MetricReading",
    "ProcessAwarenessInfo",
    "ProcessAwarenessProvider",
    "ProcessContextItem",
    "ProviderIssue",
    "ResourceMonitoringProvider",
    "ResourceUsageInfo",
    "StorageInfoProvider",
    "StorageVolumeInfo",
    "SystemContextInfo",
    "SystemInfoProvider",
    "classify_file_category",
    "is_sensitive_context_filename",
    "sanitize_untrusted_label",
]
