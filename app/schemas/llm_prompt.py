from pydantic import BaseModel, ConfigDict, Field, field_validator

# A generous ceiling: the factory default is ~1.5k chars, but a user may paste a long, detailed
# extraction prompt. Large enough not to get in the way, bounded so a single row can't be used to
# store megabytes of text.
MAX_PROMPT_LENGTH = 20_000


class LLMPromptRead(BaseModel):
    """The current user's extraction prompt as returned by ``GET /llm-prompt``."""

    model_config = ConfigDict(from_attributes=True)

    prompt_text: str


class LLMPromptWrite(BaseModel):
    """Payload for ``PUT /llm-prompt``: the user's edited prompt text."""

    prompt_text: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)

    @field_validator("prompt_text")
    @classmethod
    def _strip_and_require_non_blank(cls, value: str) -> str:
        # min_length=1 only rejects the empty string, so a whitespace-only prompt ("   ") would
        # slip through. Trim surrounding whitespace and reject a now-blank value - a blank prompt
        # would break extraction downstream.
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt_text must not be blank")
        return stripped
