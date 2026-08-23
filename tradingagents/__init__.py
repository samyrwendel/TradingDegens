import contextlib
import os
import warnings


def _load_env_file(path: str, *, override: bool = False) -> None:
    """Minimal, dependency-free ``.env`` loader (used when python-dotenv is absent).

    Parses ``KEY=VALUE`` lines, skipping blanks/comments and an optional
    ``export`` prefix, and stripping matching surrounding quotes. Never clobbers
    a value already present in the environment unless ``override`` is set — so an
    explicitly exported variable always wins.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = value


def _find_env_file(name: str) -> str | None:
    """Locate ``name`` walking up from the CWD (like ``find_dotenv(usecwd=True)``)."""
    directory = os.getcwd()
    while True:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _load_dotenv() -> None:
    """Load ``.env`` / ``.env.enterprise`` at import so DEFAULT_CONFIG's env-var
    overlay (and every llm_clients / vendor consumer, e.g. FRED_API_KEY) sees the
    user's keys regardless of which entry point started the process.

    Prefers python-dotenv when installed; otherwise falls back to a stdlib loader
    so the fork works out of the box even when the optional dependency is missing.
    That missing dependency was exactly why FRED silently fell back to
    "unavailable" unless FRED_API_KEY was exported by hand (fork brief #3).
    load order never overrides values the caller already exported.
    """
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
        load_dotenv(find_dotenv(".env.enterprise", usecwd=True), override=False)
        return
    except ImportError:
        pass

    env = _find_env_file(".env")
    if env:
        _load_env_file(env)
    enterprise = _find_env_file(".env.enterprise")
    if enterprise:
        _load_env_file(enterprise)


_load_dotenv()

# Activate the in-repo data-governance cache (DA-058): historical data is
# fetched once and kept forever; only the current day expires. Formerly an
# out-of-tree patch loaded via a venv ``.pth`` — now first-class fork code,
# imported normally. Fail-open: any error here leaves TradingAgents running
# uncached rather than blocking import. Disable with TA_DATACACHE_DISABLE=1.
with contextlib.suppress(Exception):
    from tradingagents.datacache import install as _install_datacache

    _install_datacache()

# langchain-core 1.3.3 calls surface_langchain_deprecation_warnings() in
# its own __init__, which prepends default-action filters for its
# subclassed warning categories. To suppress a specific warning we must
# install our filter AFTER langchain-core has installed its own, so import
# it first. The package is a guaranteed transitive dep via langgraph.
with contextlib.suppress(ImportError):
    import langchain_core  # noqa: F401

# langgraph-checkpoint 4.0.3 calls Reviver() at module load without an
# explicit allowed_objects, which triggers a noisy pending-deprecation
# warning from langchain-core 1.3.3 on every interpreter start. The fix
# is already merged upstream (langchain-ai/langgraph#7743, 2026-05-08)
# and will arrive in the next langgraph-checkpoint release. Remove this
# block (and the langchain_core preload above) when we bump past it.
warnings.filterwarnings(
    "ignore",
    message=r"The default value of `allowed_objects`.*",
    category=PendingDeprecationWarning,
)
