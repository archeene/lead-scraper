from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API auth
    api_secret: str = "change-me"

    # ClubReady (SZ Westborough)
    cr_username: str = ""
    cr_password: str = ""
    cr_store_id: str = ""

    # Spark Membership (IMA Westborough)
    spark_email: str = ""
    spark_password: str = ""

    # WellnessLiving (IMA Worcester)
    wl_client_id: str = ""
    wl_client_secret: str = ""
    wl_business_id: str = "697216"
    wl_location_id: str = "453099"

    # Supabase (VAP App, for booking_requests logging)
    supabase_url: str = ""
    supabase_service_key: str = ""

    # Staleness threshold (days since last contact to count as "stale")
    stale_days: int = 30

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
