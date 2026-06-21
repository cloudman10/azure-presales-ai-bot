from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LLM_PROVIDER: "foundry" (default, Azure AI Foundry / GPT-4o) or "anthropic".
    # Switch to "anthropic" + add ANTHROPIC_API_KEY when Anthropic is available on the Azure subscription.
    llm_provider: str = "foundry"
    anthropic_api_key: str = ""   # optional — only read when llm_provider="anthropic"
    environment: str = "dev"
    port: int = 8000

    class Config:
        env_file = ".env"


settings = Settings()
