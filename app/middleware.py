"""Response headers that constrain what a browser will do with our pages.

Dashboards are public and render strings that arrived from the network (device
names, sensor names, project names). The templates autoescape and the
front-end assigns through `textContent`, so there is no injection today — these
headers are the second layer that limits the damage if that ever slips.

`default-src 'self'` is only viable because the app has no inline scripts and
vendors ECharts locally. If you point `ECHARTS_SRC` at a CDN, that origin has
to be added to `script-src` or the dashboard will silently fail to draw.
"""
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings


def _content_security_policy() -> str:
    script_src = ["'self'"]
    # A configured CDN is an explicit operator decision; honour it rather than
    # shipping a policy that breaks the charts.
    parsed = urlparse(settings.echarts_src)
    if parsed.scheme and parsed.netloc:
        script_src.append(f"{parsed.scheme}://{parsed.netloc}")

    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {' '.join(script_src)}",
            # ECharts sets element styles programmatically, which counts as
            # inline style, so 'unsafe-inline' is required here. It carries far
            # less risk than the script-src equivalent.
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", _content_security_policy())
        headers.setdefault("X-Content-Type-Options", "nosniff")
        # frame-ancestors above covers modern browsers; this covers the rest.
        headers.setdefault("X-Frame-Options", "DENY")
        # Device IDs live in dashboard URLs, so keep them out of Referer
        # headers sent to other origins.
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response
