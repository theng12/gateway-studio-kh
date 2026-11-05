"""Authenticated Gateway helper; bind KH_GATEWAY_URL explicitly before any call."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def key():
    with open(os.path.expanduser("~/Library/Application Support/gateway-kh/.env")) as stream:
        for line in stream:
            if line.startswith("GATEWAY_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"\'')
    raise RuntimeError("Gateway API key is not configured.")


def call(method, path, body=None):
    origin = os.environ.get("KH_GATEWAY_URL", "").strip()
    if not origin:
        raise ValueError("Bind KH_GATEWAY_URL explicitly; no default Gateway is selected.")
    try:
        parsed = urllib.parse.urlsplit(origin)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or "?" in origin or "#" in origin
                or any(char.isspace() or ord(char) < 32 for char in origin)):
            raise ValueError
        if parsed.port is not None and parsed.port < 1:
            raise ValueError
    except ValueError:
        raise ValueError("KH_GATEWAY_URL must be an HTTP(S) origin without credentials, "
                         "a path, query or fragment.") from None
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Gateway request path must be relative to the bound origin.")
    origin = origin.rstrip("/")
    token = key()
    headers = {"Authorization": "Bearer " + token, "X-API-Key": token,
               "Content-Type": "application/json", "X-Gateway-Origin": origin}
    request = urllib.request.Request(origin + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:500]


if __name__ == "__main__":
    status, data = call(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else None)
    print(status)
    print(json.dumps(data, indent=1)[:int(os.environ.get("MAXC", "6000"))])
