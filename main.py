from pathlib import Path

from dotenv import load_dotenv

# Load .env from several candidate locations so the SDK credentials are
# available regardless of the CWD when the plugin is launched.
_here = Path(__file__).resolve().parent
for _candidate in (_here / ".env", _here.parent / ".env", Path(".env")):
    if _candidate.is_file():
        load_dotenv(dotenv_path=str(_candidate))

from dify_plugin import Plugin, DifyPluginEnv

plugin = Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=3600))

if __name__ == '__main__':
    plugin.run()
