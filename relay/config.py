"""Gateway configuration. No development fallback is enabled in production."""
from dataclasses import dataclass
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    database_url: str
    allow_insecure: bool = False
    request_timeout: float = 30
    transfer_timeout: float = 60
    session_days: int = 30
    idle_days: int = 7
    max_file_bytes: int = 100 * 1024 * 1024
    max_body_bytes: int = 15 * 1024 * 1024

    def __post_init__(self):
        url = urlsplit(self.public_base_url)
        if (url.scheme not in ('https', 'http') or not url.netloc or url.username or
                url.password or url.path or url.query or url.fragment or
                (url.scheme != 'https' and not self.allow_insecure)):
            raise ValueError('PUBLIC_BASE_URL must be a canonical HTTPS origin without trailing slash')
        if not self.database_url:
            raise ValueError('DATABASE_URL is required')

    @classmethod
    def from_env(cls):
        return cls(os.environ['PUBLIC_BASE_URL'], os.environ['DATABASE_URL'],
                   allow_insecure=os.environ.get('ALLOW_INSECURE_LOCAL_TEST') == '1')
