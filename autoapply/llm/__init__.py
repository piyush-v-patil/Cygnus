from .client import LLM, AnthropicTransport, BudgetExceeded, FakeTransport, LLMError


def make_llm(profile, ledger, secrets, transport=None) -> LLM:
    if transport is None:
        key = secrets.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set (secrets.env)")
        transport = AnthropicTransport(key)
    return LLM(profile.llm, ledger, transport)


__all__ = ["LLM", "AnthropicTransport", "BudgetExceeded", "FakeTransport", "LLMError", "make_llm"]
