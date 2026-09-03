"""Vercel entry point for the Cynea control-plane API.

Vercel's Python runtime looks for a module-level ASGI app named `app`, so
this file's whole job is to put the existing FastAPI application there.
Everything it serves lives in `cynea/api.py`; nothing is redefined here.

    /api/health          -> cynea.api health()
    /api/auth/login      -> cynea.api login()
    /api/agents          -> cynea.api list_agents() ...

Why one file and not five
-------------------------
The plan asked for api/login.py, api/agents.py, api/calls.py and
api/dashboard.py alongside this. That is the right shape for bare handler
functions and the wrong one for FastAPI, which is itself a router: each
file would have to construct its own FastAPI instance, so a request would
boot a separate process with a separate database pool and a separate cold
start, and the four apps would disagree about which one owns a path.
`vercel.json` rewrites every /api/* request here instead, and FastAPI does
the routing it already knows how to do — one function, one pool, one place
where auth is checked.

Path prefix
-----------
The rewrite preserves the whole path, so a request arrives as
`/api/agents` while `cynea.api` declares the route as `/agents`. The inner
app is therefore mounted under `/api`, which strips the prefix before
matching — assigning `app.root_path` after construction does not, because
routing reads the prefix out of the ASGI scope rather than the attribute,
and every route would 404.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The function's working directory is the deployment root, but the
# repository root is this file's parent — make `import cynea` resolve the
# same way it does locally.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse   # noqa: E402
from starlette.routing import Mount, Route     # noqa: E402

from cynea.api import app as control_plane     # noqa: E402


async def _root(_request):
    """Anything that reaches the function outside /api."""
    return JSONResponse(
        {"service": "cynea-api", "docs": "/api/docs", "health": "/api/health"}
    )


app = Starlette(routes=[
    Mount("/api", app=control_plane),
    Route("/{path:path}", endpoint=_root),
])
