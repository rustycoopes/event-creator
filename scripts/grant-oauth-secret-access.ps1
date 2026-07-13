<#
Grants the Cloud Run runtime service account Secret Manager access to the four OAuth
client-secret secrets used by deploy-qa/deploy-prod (Slice R7). Run this once after
creating the secrets themselves (gcloud secrets create ...) - see event-creator/README.md's
"Human setup required before deploy-qa/deploy-prod will succeed" section.
#>

$ProjectId = "gen-lang-client-0791944342"
$ServiceAccount = "170051512639-compute@developer.gserviceaccount.com"
$Secrets = @(
    "google-oauth-client-secret-qa",
    "google-oauth-client-secret-prod",
    "dropbox-oauth-client-secret-qa",
    "dropbox-oauth-client-secret-prod"
)

foreach ($secret in $Secrets) {
    Write-Host "Granting secretAccessor on $secret to $ServiceAccount..."
    gcloud secrets add-iam-policy-binding $secret `
        --project $ProjectId `
        --member="serviceAccount:$ServiceAccount" `
        --role="roles/secretmanager.secretAccessor"
}
