"""Browser-free Jupyter URL helpers."""

from __future__ import annotations


def jupyter_server_base(lab_url: str) -> str:
    """Derive the Jupyter server base URL from a lab frame URL.

    Only strips ``/lab`` when it is the **final** path segment (the
    JupyterLab UI route), not when ``/lab/`` appears mid-path as part
    of the platform's proxy path (e.g. ``/api/v2/notebook/lab/{id}/``).
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(lab_url)
    path = parts.path.rstrip("/")
    if path.endswith("/lab"):
        path = path[:-4]
    if not path.endswith("/"):
        path = path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def extract_jupyter_token(lab_url: str) -> str | None:
    from urllib.parse import parse_qs, urlsplit

    parsed = urlsplit(lab_url)
    query_token = parse_qs(parsed.query).get("token", [None])[0]
    if query_token:
        return query_token

    path_parts = [part for part in parsed.path.split("/") if part]
    try:
        jupyter_index = path_parts.index("jupyter")
        if len(path_parts) > jupyter_index + 2:
            return path_parts[jupyter_index + 2]
    except ValueError:
        return None
    return None


def build_terminal_websocket_url(lab_url: str, term_name: str) -> str:
    from urllib.parse import urlencode, urlsplit, urlunsplit

    base = jupyter_server_base(lab_url)
    parsed = urlsplit(base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    ws_path = f"{base_path}terminals/websocket/{term_name}"

    token = extract_jupyter_token(lab_url)
    query = urlencode({"token": token}) if token else ""
    return urlunsplit((scheme, parsed.netloc, ws_path, query, ""))
