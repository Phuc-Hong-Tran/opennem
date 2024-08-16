"""
HTTP module with custom timeout and retry adaptors

usage:

from opennem.utils.http import http
http.get(`url`) etc.

"""

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request

import logfire
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from opennem import settings
from opennem.utils.version import get_version

urllib3.disable_warnings()

logfire.instrument_requests()

logger = logging.getLogger("opennem.utils.http")

DEFAULT_TIMEOUT = settings.http_timeout
DEFAULT_RETRIES = settings.http_retries

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36"  # noqa: 501
)

CHROME_AGENT_HEADERS = {
    "user-agent": USER_AGENT,
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "accept-encoding": "gzip, deflate",
    "scheme": "https",
    "sec-ch-prefers-color-scheme": "light",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

API_CLIENT_HEADERS = {"user-agent": f"OpenNEM (v {get_version()})", "accept": "*/*"}

HTTP_CACHE_SETUP = False


def setup_http_cache() -> bool:
    """Sets up requests session local cachine using
    requests-cache if enabled in settings"""
    global HTTP_CACHE_SETUP

    if HTTP_CACHE_SETUP:
        return True

    if not settings.http_cache_local:
        return False

    try:
        import requests_cache
    except ImportError:
        logger.error("Request caching requires requests-cache library")
        return False

    requests_cache.install_cache(".opennem_requests_cache", expire_after=60 * 60 * 4)
    logger.info(f"Setup HTTP cache at: {settings.http_cache_local}")

    HTTP_CACHE_SETUP = True

    return True


class TimeoutHTTPAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        self.timeout = DEFAULT_TIMEOUT
        if "timeout" in kwargs:
            self.timeout = kwargs["timeout"]
            del kwargs["timeout"]
        super().__init__(*args, **kwargs)

    def send(self, request: Request, **kwargs) -> Any:  # Add type annotation for return value
        timeout = kwargs.get("timeout")
        if timeout is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


retry_strategy = Retry(
    total=DEFAULT_RETRIES,
    backoff_factor=2,
    status_forcelist=[403, 429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS"],
)


# This will retry on 403's as well
retry_strategy_on_permission_denied = Retry(
    total=DEFAULT_RETRIES,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS"],
)

http = requests.Session()

http.headers.update({"User-Agent": USER_AGENT})

adapter_timeout = TimeoutHTTPAdapter()
http.mount("https://", adapter_timeout)
http.mount("http://", adapter_timeout)


adapter_retry = HTTPAdapter(max_retries=retry_strategy)
http.mount("https://", adapter_retry)
http.mount("http://", adapter_retry)

setup_http_cache()


def mount_timeout_adaptor(session: requests.Session) -> None:
    session.mount("https://", adapter_timeout)
    session.mount("http://", adapter_timeout)


def mount_retry_adaptor(session: requests.Session) -> None:
    session.mount("https://", adapter_retry)
    session.mount("http://", adapter_retry)


def attach_proxy(session: requests.Session) -> requests.Session:
    """Attach setup proxy info to the session"""
    if not settings.http_proxy_url:
        logger.warning("Attempting to attach proxy with no settings set")
        return session

    proxies = {
        "http": settings.http_proxy_url,
        "https": settings.http_proxy_url,
    }

    session = requests.Session()
    session.proxies.update(proxies)  # type: ignore

    return session


def test_proxy() -> None:
    """Display test proxy output"""
    if not settings.https_proxy_url:
        logger.debug("Attempting to test proxy with no settings set")
        return None

    req = requests.Session()
    req = attach_proxy(req)
    url = "http://lumtest.com/myip.json"

    resp = req.get(url, verify=False)

    if not resp.ok:
        logger.error("Error in test_proxy")

    resp_envelope = resp.json()

    logger.info("Using proxy with IP {} and country {}".format(resp_envelope["ip"], resp_envelope["country"]))


def download_file(url: str, destination_directory: Path, expect_content_type: str | None = None) -> Path | None:
    """Downloads a file from a URL to a destination directory"""
    if not destination_directory.exists():
        destination_directory.mkdir(parents=True)

    local_filename = urlparse(url).path.split("/")[-1]

    local_path = destination_directory / local_filename

    if local_path.exists():
        logger.info(f"File exists: {local_path}")
        return local_path

    logger.info(f"Downloading file: {local_filename}")

    r = http.get(url, stream=True)

    if not r.ok or not r.status_code == 200:
        logger.error(f"Error {r.status_code} downloading file: {local_filename}")
        return None

    if expect_content_type and r.headers["Content-Type"] != expect_content_type:
        logger.error(f"Content type mismatch: {r.headers["Content-Type"]} != {expect_content_type}")
        return None

    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:
                f.write(chunk)

    return local_path
