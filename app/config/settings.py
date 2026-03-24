from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str
    environment: str = "dev"
    port: int = 8000

    class Config:
        env_file = ".env"


settings = Settings()
