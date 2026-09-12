"""Windows desktop shell for the existing FastAPI dashboard.

This package owns exactly two things the web app never had: the lifetime of
a locally bound backend, and a native window pointed at it. Everything else
-- routes, caches, providers, persistence, diagnostics -- stays in `app.*`
and is reused untouched.

Nothing here is imported by the FastAPI application, and importing this
package never imports pywebview: the GUI dependency is pulled in only when a
window is actually opened (`app.desktop.__main__`), so the backend and its
tests keep running with no GUI installed.
"""
