"""Copy the shared voice pipeline out of the repo and into src/.

Run by wrangler as the [build] command, so it happens on every
`pywrangler dev` and `pywrangler deploy` — there is no separate step to
forget. The copies are gitignored; cynea/ and cynea_africa/ at the repo
root stay the only source of truth.

Why copy at all
---------------
Wrangler bundles the directory that holds the main module. Pointing
`base_dir` at the repo root instead was tried and measured: it swept in
464 modules, 236 of them from the local .venv-workers, plus every tool,
test and HTML page in the repo. An allowlist is the opposite failure
mode — a module the pipeline needs and this list forgets — and that one
fails loudly at import, on the first `pywrangler dev`.

What is deliberately left out: the FastAPI app (cynea/api.py, voice_ws.py),
the Groq LLM adapter (cynea/llms — replies come from Cerebras, in
src/main.py), the database layer, local Whisper, ElevenLabs, telephony, and
the 5,560-line dashboard preview. cynea/providers.py guards each of those imports, so their
absence just leaves them unregistered.
"""

from __future__ import annotations

import pathlib

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
SRC = HERE / "src"

SHARED = [
    "cynea/__init__.py",
    "cynea/models.py",
    "cynea/conversation.py",
    "cynea/interruption.py",
    "cynea/engine.py",
    "cynea/providers.py",
    "cynea/agent_loader.py",
    "cynea/voice_demo.py",
    "cynea/monitoring.py",
    "cynea_africa/__init__.py",
    "cynea_africa/theme.py",
    "cynea_africa/persona/__init__.py",
    "cynea_africa/persona/amina.py",
    "cynea_africa/persona/kofi.py",
    "cynea_africa/persona/kwame.py",
    "cynea_africa/persona/maya.py",
    "cynea_africa/transcriber/__init__.py",
    "cynea_africa/transcriber/groq_stt.py",
    "cynea_africa/synthesizer/__init__.py",
    "cynea_africa/synthesizer/edge_tts.py",
]


def main() -> None:
    wanted = {SRC / rel for rel in SHARED}

    # Only write what changed. Wrangler re-runs this when its watched
    # directories change, so rewriting identical files would be a loop.
    written = 0
    for rel in SHARED:
        src = REPO / rel
        if not src.is_file():
            raise SystemExit(f"sync_shared: {rel} is missing from the repo")
        dst = SRC / rel
        data = src.read_bytes()
        if dst.is_file() and dst.read_bytes() == data:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        written += 1

    # A module dropped from the list must not linger in the bundle.
    for pkg in ("cynea", "cynea_africa"):
        for stale in (SRC / pkg).rglob("*.py") if (SRC / pkg).exists() else ():
            if stale not in wanted:
                stale.unlink()
        # And neither may its directory: an empty one still imports, as a
        # namespace package, so `import cynea.llms` would quietly succeed.
        for d in sorted((SRC / pkg).rglob("*"), reverse=True) if (SRC / pkg).exists() else ():
            if d.is_dir() and not any(p.suffix == ".py" for p in d.rglob("*")):
                for leftover in d.rglob("*"):
                    if leftover.is_file():
                        leftover.unlink()
                for sub in sorted(d.rglob("*"), reverse=True):
                    sub.rmdir()
                d.rmdir()

    print(f"sync_shared: {len(SHARED)} shared modules, {written} updated")


if __name__ == "__main__":
    main()
