from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = True

    database_url: str = "sqlite+aiosqlite:///./vk_osb.db"

    vk_group_id: int = 0
    vk_group_token: str = ""
    vk_confirmation_token: str = ""
    vk_longpoll_wait: int = 25

    admin_id: int = 0
    log_peer_id: int = 0

    media_base_url: str = ""
    secret_key: str = "change_me"


settings = Settings()
