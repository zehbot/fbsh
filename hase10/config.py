"""Settings for the FRITZ!Box connection.

Resolution order: command line > environment > credentials file.

    FRITZ_HOST       fritz.box            box name or IP
    FRITZ_USER       (empty)              FRITZ!Box user, needs "Smart Home" permission
    FRITZ_PASSWORD   -                    password
    FRITZ_SCHEME     http | https
    FRITZ_PORT       -                    explicit port (default: none)
    FRITZ_VERIFY_TLS 1                    verify the box certificate (with FRITZ_CA_FILE)
    FRITZ_CA_FILE    -                    PEM of the box certificate

Credentials file (default ~/.config/hase10/credentials, mode 600):

    user=hase10
    password=...

The password is never logged and never written by this package.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CREDENTIALS = Path.home() / ".config" / "hase10" / "credentials"


class ConfigError(Exception):
    pass


def _read_credentials(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{path} is readable by others (mode {mode:o}); run: chmod 600 {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().lower()] = value.strip()
    return values


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

    @classmethod
    def load(cls, host=None, user=None, password=None, scheme=None, port=None,
             verify_tls=None, ca_file=None, credentials_file=None) -> "Settings":
        file_values = _read_credentials(
            Path(credentials_file).expanduser() if credentials_file else DEFAULT_CREDENTIALS
        )

        def pick(explicit, env_name, file_key, default):
            if explicit not in (None, ""):
                return explicit
            env = os.environ.get(env_name)
            if env not in (None, ""):
                return env
            if file_key in file_values:
                return file_values[file_key]
            return default

        settings = cls(
            host=pick(host, "FRITZ_HOST", "host", "fritz.box"),
            user=pick(user, "FRITZ_USER", "user", ""),
            password=pick(password, "FRITZ_PASSWORD", "password", ""),
            scheme=pick(scheme, "FRITZ_SCHEME", "scheme", "http"),
            port=None,
            verify_tls=False,
        )
        raw_port = pick(port, "FRITZ_PORT", "port", None)
        if raw_port not in (None, ""):
            try:
                settings.port = int(str(raw_port))
            except ValueError as exc:
                raise ConfigError(f"invalid port: {raw_port}") from exc

        raw_verify = pick(verify_tls, "FRITZ_VERIFY_TLS", "verify_tls", None)
        if raw_verify is not None:
            settings.verify_tls = str(raw_verify).lower() in ("1", "true", "yes", "on")
        settings.ca_file = pick(ca_file, "FRITZ_CA_FILE", "ca_file", None)

        if settings.scheme not in ("http", "https"):
            raise ConfigError(f"scheme must be http or https, not {settings.scheme}")
        if not settings.password:
            raise ConfigError(
                "no password configured - set FRITZ_PASSWORD, pass --password, "
                f"or write user/password to {DEFAULT_CREDENTIALS} (mode 600)"
            )
        return settings
