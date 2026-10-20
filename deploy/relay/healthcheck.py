"""Check the internal service while preserving its canonical Host allowlist."""
import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    host = urlsplit(os.environ["PUBLIC_BASE_URL"]).netloc
    request = Request("http://127.0.0.1:8000/healthz", headers={"Host": host})
    with urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
