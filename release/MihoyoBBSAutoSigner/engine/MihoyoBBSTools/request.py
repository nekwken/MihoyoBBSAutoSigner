import sys
import time


RETRY_TIMES = 3
RETRY_BACKOFF = 2


def _with_retry(do_request):
    last_error = None
    for attempt in range(RETRY_TIMES):
        try:
            return do_request()
        except Exception as e:
            # 仅重试传输层抖动（连接被重置/超时等），HTTP 状态错误不重试
            if _is_transient(e):
                last_error = e
                time.sleep(RETRY_BACKOFF * (attempt + 1))
            else:
                raise
    raise last_error


def _is_transient(e: Exception) -> bool:
    try:
        import httpx
        if isinstance(e, httpx.TransportError):
            return True
    except ModuleNotFoundError:
        pass
    try:
        import requests
        if isinstance(e, (requests.ConnectionError, requests.Timeout)):
            return True
    except ModuleNotFoundError:
        pass
    return False


def get_new_session(**kwargs):
    try:
        # 优先使用httpx，在httpx无法使用的环境下使用requests
        import httpx

        http_client = httpx.Client(timeout=30, transport=httpx.HTTPTransport(retries=10), follow_redirects=True,
                                   **kwargs)
        # 当openssl版本小于1.0.2的时候直接进行一个空请求让httpx报错
        import tools

        if tools.get_openssl_version() < 102:
            httpx.get()
    except (TypeError, ModuleNotFoundError) as e:
        import requests
        from requests.adapters import HTTPAdapter

        session = requests.Session()
        session.mount('http://', HTTPAdapter(max_retries=10))
        session.mount('https://', HTTPAdapter(max_retries=10))
        http_client = _RequestsRetryShim(session)
    return _RetryShim(http_client)


def is_module_imported(module_name):
    return module_name in sys.modules


def get_new_session_use_proxy(http_proxy: str):
    if is_module_imported("httpx"):
        proxies = {
            "http://": f'http://{http_proxy}',
            "https://": f'http://{http_proxy}'
        }
        return get_new_session(proxies=proxies)
        # httpx 版本大于0.26.0可用
        # return get_new_session(proxy=f'http://{http_proxy}')
    else:
        session = get_new_session()
        session.proxies = {
            "http": f'http://{http_proxy}',
            "https": f'http://{http_proxy}'
        }
        return session


class _RetryShim:
    """对 get/post/request 做传输层重试，保持原有 Session 接口。"""

    def __init__(self, client) -> None:
        self._client = client

    def get(self, *args, **kwargs):
        return _with_retry(lambda: self._client.get(*args, **kwargs))

    def post(self, *args, **kwargs):
        return _with_retry(lambda: self._client.post(*args, **kwargs))

    def request(self, *args, **kwargs):
        return _with_retry(lambda: self._client.request(*args, **kwargs))

    def __getattr__(self, item):
        return getattr(self._client, item)


class _RequestsRetryShim:
    def __init__(self, session) -> None:
        self._session = session

    def __getattr__(self, item):
        return getattr(self._session, item)


http = get_new_session()
