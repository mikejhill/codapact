"""Stand-in browser for the PKCE test: ``BROWSER="python fake_browser.py %s"``.

Requests the authorization URL, then follows the redirect to the CLI's loopback
listener, as a person approving the sign-in would.
"""

import sys
import urllib.error
import urllib.request
from typing import override


class NoRedirect(urllib.request.HTTPRedirectHandler):
    @override
    def redirect_request(self, *args, **kwargs):
        return None


opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
try:
    opener.open(sys.argv[1], timeout=10)
except urllib.error.HTTPError as error:
    location = error.headers.get("Location")
    if error.code != 302 or not location:
        raise
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(location, timeout=10).read()
