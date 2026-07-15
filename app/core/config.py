from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    database_url: str
    # Same signing key as the Host (Secret Manager secret `jwt-secret-qa`/`jwt-secret-prod`, R4) —
    # Event Creator verifies the Host-issued JWT, it never issues one of its own.
    jwt_secret: str
    # Google Drive OAuth app credentials (Slice R7, ported from organize-me's storage-connect
    # flow). Empty defaults so deploys/CI that don't set them yet don't fail Settings
    # construction - the connect flow only needs real values once a user actually clicks Connect.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # The Google Drive OAuth callback's absolute redirect_uri (issue #200), fixed per environment
    # rather than derived from the incoming request's Host header. Google rejects a redirect_uri
    # that doesn't exactly match one registered on the OAuth client with
    # `Error 400: redirect_uri_mismatch` - deriving it from request.base_url meant the value
    # silently tracked whatever domain/service happened to receive the request (the raw Cloud Run
    # URL before the R5 load-balancer cutover, this service's own URL once Storage moved here in
    # R7), never landing on one fixed value an operator could register in Google Cloud Console.
    # Empty default (mirrors the other optional secrets above) - /auth fails fast with a clear
    # error if it's actually used while unset, rather than sending the user through the whole
    # Google consent flow only to have Google reject it.
    google_drive_redirect_uri: str = ""
    # Dropbox OAuth app credentials (Slice R7). Same empty-default reasoning.
    dropbox_oauth_client_id: str = ""
    dropbox_oauth_client_secret: str = ""
    # Fernet key used to encrypt stored storage-provider credentials at rest (see
    # app.core.security). Empty default (mirrors organize-me's ENCRYPTION_KEY) - the storage
    # connect flows raise a clear, actionable RuntimeError if it's actually used while unset.
    # Must be a urlsafe-base64 32-byte key (cryptography.fernet.Fernet.generate_key()).
    encryption_key: str = ""
    # Enables the E2E fake storage provider in app.services.storage.factory.build_storage_provider
    # (mirrors organize-me's flag). Defaults false; no route in this slice reads it yet, but the
    # ported factory function's signature depends on it existing on Settings.
    e2e_test_mode: bool = False
    # Gemini API key used by app.services.llm.gemini.GoogleGeminiClient (ported from organize-me,
    # issue #51). Empty default (mirrors the other optional secrets above) - GoogleGeminiClient
    # raises a clear GeminiError if it's actually used while unset.
    gemini_api_key: str = ""
    # Resend API key used by app.services.notifications.email.ResendEmailSender (ported from
    # organize-me). Empty default (mirrors the other optional secrets above) - ResendEmailSender
    # raises a clear RuntimeError if it's actually used while unset.
    resend_api_key: str = ""
    # Swap via EMAIL_FROM once a custom domain is verified.
    email_from: str = "OrganizeMe <onboarding@resend.dev>"
    # Base URL used to build links (dashboard, run logs) in notification emails/SMS. Defaults to
    # https://organize-me.app for production; override to http://localhost:3000 in dev.
    base_url: str = "https://organize-me.app"
    # Twilio credentials used by app.services.notifications.sms.TwilioSmsSender (ported from
    # organize-me). Empty defaults - TwilioSmsSender raises a clear error if it's actually used
    # while unset.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    # Cloud Tasks dispatch (replaces Celery/Redis, see docs/adr/0001 in organize-me): the queue
    # the pipeline is dispatched through, and the identity/URL Cloud Tasks uses to push tasks back
    # to this same service's /internal/pipeline/run. All non-sensitive - plain Cloud Run env vars,
    # not Secret Manager. Empty defaults mirror every other optional-until-wired field above; the
    # dispatch code raises a clear error if it's actually used while unset.
    gcp_project_id: str = ""
    cloud_tasks_location: str = ""
    cloud_tasks_queue: str = ""
    pipeline_invoker_service_account: str = ""
    # This service's own Cloud Run URL (its https://*.run.app address, captured at deploy time -
    # see deploy.yml) - deliberately distinct from base_url above, which is the public shared LB
    # domain. Cloud Tasks pushes directly to the Cloud Run URL, bypassing the LB, so the OIDC
    # audience and the push target both need this service's real address, not the shared domain.
    pipeline_endpoint_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
