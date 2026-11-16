"""Preserve the socket peer for the application's TRUSTED_PROXIES policy."""
from uvicorn.workers import UvicornWorker

class GeocoderWorker(UvicornWorker):
    CONFIG_KWARGS = {**UvicornWorker.CONFIG_KWARGS, 'proxy_headers': False}
