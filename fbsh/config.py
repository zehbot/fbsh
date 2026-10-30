"""Settings for the FRITZ!Box connection.

Resolution order, first hit wins:

    1. command line arguments
    2. process environment (FRITZ_*)
    3. a .env file: private/.env, then ./.env, then $FBSH_ENV_FILE

Recognised keys:

    FRITZ_HOST       fritz.box
    FRITZ_USER       FRITZ!Box user with the "Smart Home" permission
    FRITZ_PASSWORD   the box password
    FRITZ_SCHEME     http | https
    FRITZ_PORT       explicit port
    FRITZ_VERIFY_TLS 1 to verify the box certificate (with FRITZ_CA_FILE)
    FRITZ_CA_FILE    PEM of the box certificate

The .env file is parsed, never sourced: `KEY=value`, `export KEY=value`,
quotes, blank lines and `#` comments are understood. Values are never logged.

On a plain host the file must not be readable by group or others (mode 0600).
Inside a container that check is skipped, because Kubernetes mounts secrets
with 0440 by design.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "FBSH_ENV_FILE"
PERMISSIVE_VAR = "FBSH_ENV_PERMISSIVE"

# searched in this order, relative to the working directory and to the project root
DEFAULT_NAMES = ("private/.env", ".env")

class ConfigError(Exception):
    pass


def in_container() -> bool:
    """True when we are most likely running inside a container or pod."""
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def find_env_file(explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        path = Path(from_env).expanduser()
        return path if path.is_file() else None
    for base in (Path.cwd(), project_root()):
        for name in DEFAULT_NAMES:
            candidate = base / name
            if candidate.is_file():
                return candidate
    return None


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Keys are upper-cased."""
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip().upper()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def check_permissions(path: Path) -> None:
    """Refuse a world/group readable .env on a normal host."""
    if in_container() or os.environ.get(PERMISSIVE_VAR) == "1":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        raise ConfigError(
            f"{path} is readable by others (mode {mode:o}); run: chmod 600 {path}"
        )


@dataclass
class Settings:
    host: str = "fritz.box"
    user: str = ""
    password: str = ""
    scheme: str = "http"
    port: int | None = None
    verify_tls: bool = False
    ca_file: str | None = None
    timeout: float = 10.0
    env_file: Path | None = None

    @classmethod
    def load(
        cls,
        host=None,
        user=None,
        password=None,
        scheme=None,
        port=None,
        verify_tls=None,
        ca_file=None,
        env_file=None,
    ) -> "Settings":
        path = find_env_file(env_file)
        file_values: dict[str, str] = {}
        if path is not None:
            check_permissions(path)
            file_values = parse_env_file(path)

        def pick(explicit, env_name, default):
            if explicit not in (None, ""):
                return explicit
            env = os.environ.get(env_name)
            if env not in (None, ""):
                return env
            if env_name in file_values and file_values[env_name] != "":
                return file_values[env_name]
            return default

        settings = cls(
            host=pick(host, "FRITZ_HOST", "fritz.box"),
            user=pick(user, "FRITZ_USER", ""),
            password=pick(password, "FRITZ_PASSWORD", ""),
            scheme=pick(scheme, "FRITZ_SCHEME", "http"),
            verify_tls=False,
            env_file=path,
        )

        raw_port = pick(port, "FRITZ_PORT", None)
        if raw_port not in (None, ""):
            try:
                settings.port = int(str(raw_port))
            except ValueError as exc:
                raise ConfigError(f"invalid FRITZ_PORT: {raw_port}") from exc

        raw_verify = pick(verify_tls, "FRITZ_VERIFY_TLS", None)
        if raw_verify is not None:
            settings.verify_tls = str(raw_verify).lower() in ("1", "true", "yes", "on")
        settings.ca_file = pick(ca_file, "FRITZ_CA_FILE", None)

        if settings.scheme not in ("http", "https"):
            raise ConfigError(f"FRITZ_SCHEME must be http or https, not {settings.scheme}")
        if not getattr(settings, "password"):
            raise ConfigError(
                "no credentials configured - set FRITZ_PASSWORD, pass it on the command line, "
                "or put it into private/.env"
            )
        return settings
