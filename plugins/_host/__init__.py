"""plugins/ 内共享的社交平台 Channel 插件宿主。"""

from ports.egress import (
    EgressEnvelope,
    EgressHandler,
    EgressPlugin,
    EgressPluginContext,
)

from .capabilities import (
    WEB_SIMULATOR_CAPABILITIES,
    CapabilityStatus,
    ChannelCapabilityMatrix,
    ChannelCapabilityRegistry,
)
from .contracts import (
    ChannelPlugin,
    GroupManagementChannel,
    HttpCallbackRequest,
    HttpCallbackResponse,
    InboundEnvelope,
    IngressHandler,
    IngressPlugin,
    IngressPluginContext,
    MessageReference,
    PhasedPluginLifecycle,
    PluginContext,
    PluginUnavailableError,
    RuntimeContextChannel,
    TypingEvent,
    TypingHandler,
)
from .contributions import (
    ManagedServiceSpec,
    PluginContribution,
    PluginContributionCatalog,
    RuntimePluginManifest,
)
from .ingress import PlatformIngress
from .managed_services import (
    ManagedServiceClient,
    ManagedServiceError,
    ManagedServiceManager,
)
from .manager import ChannelPluginManager
from .router import ChannelRouter

__all__ = [
    "ChannelPlugin",
    "CapabilityStatus",
    "ChannelCapabilityMatrix",
    "ChannelCapabilityRegistry",
    "ChannelPluginManager",
    "ChannelRouter",
    "EgressEnvelope",
    "EgressHandler",
    "EgressPlugin",
    "EgressPluginContext",
    "HttpCallbackRequest",
    "HttpCallbackResponse",
    "GroupManagementChannel",
    "InboundEnvelope",
    "IngressHandler",
    "IngressPlugin",
    "IngressPluginContext",
    "ManagedServiceClient",
    "ManagedServiceError",
    "ManagedServiceManager",
    "ManagedServiceSpec",
    "MessageReference",
    "PhasedPluginLifecycle",
    "PlatformIngress",
    "PluginContribution",
    "PluginContributionCatalog",
    "PluginContext",
    "PluginUnavailableError",
    "RuntimePluginManifest",
    "RuntimeContextChannel",
    "TypingEvent",
    "TypingHandler",
    "WEB_SIMULATOR_CAPABILITIES",
]
