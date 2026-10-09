from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError


class ClaudeAgentProvider(ToolProvider):
    """Provider for the Claude_Agent plugin.

    Credentials (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, …) are configured
    in the Dify UI and passed to the agent tool via ``self.runtime.credentials``.
    The tool forwards them to the claude_agent_sdk subprocess as environment
    variables so the SDK can connect to the Anthropic-compatible API.
    """

    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        token = credentials.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        if not token:
            raise ToolProviderCredentialValidationError(
                "ANTHROPIC_AUTH_TOKEN is required. "
                "Please provide your API token in the provider credentials."
            )
