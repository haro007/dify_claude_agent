# Privacy Policy

The Claude_Agent plugin processes user queries and uploaded files locally within
the plugin runtime and forwards the query text to the configured Claude API
endpoint (via `claude_agent_sdk`).

- **User input**: the `query` parameter is sent to the Claude API as configured
  by the plugin host environment variables (`ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN`).
- **Uploaded files**: files provided via the `files` parameter are downloaded
  into the plugin's `workspace/uploads/` directory and announced to the agent
  via the system prompt so the SDK's built-in tools can read them.
- **Skill packages**: skill zip packages uploaded via the Skill Manager tool are
  extracted into the plugin's `skills/` directory and stored on the plugin host.

No data is collected, transmitted to third parties, or used for training.
