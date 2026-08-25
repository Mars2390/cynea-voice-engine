"""Cynea Voice Engine — LLM adapters.

Importing this package registers every adapter whose dependencies are
importable. `cynea.providers` imports it at module load, so simply having
`cynea.providers` in scope is enough for `get_llm_provider(...)` to resolve
a real model.

Registered names
----------------
    groq        GroqLLM  — llama-3.3-70b-versatile over Groq's OpenAI-shaped API
    anthropic   alias    — see the note in groq_llm.py; kept so that existing
                           configs defaulting to "anthropic" keep working
    mock        MockLLM  — registered by cynea.providers itself
"""

from cynea.llms.groq_llm import GroqLLM, register as _register_groq

__all__ = ["GroqLLM"]

_register_groq()
