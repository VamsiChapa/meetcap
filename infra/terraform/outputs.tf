output "storage_account_url" {
  value = azurerm_storage_account.this.primary_blob_endpoint
}

output "speech_endpoint" {
  value = "https://${azurerm_cognitive_account.speech.custom_subdomain_name}.cognitiveservices.azure.com"
}

output "openai_endpoint" {
  value = var.create_openai ? "https://${azurerm_cognitive_account.openai[0].custom_subdomain_name}.openai.azure.com" : "(reuse your existing Azure OpenAI endpoint)"
}

output "meetcap_config_snippet" {
  description = "Paste into ~/Library/Application Support/meetcap/config.yaml under azure:"
  value       = <<-EOT
    azure:
      storage:
        account_url: ${trimsuffix(azurerm_storage_account.this.primary_blob_endpoint, "/")}
        container: ${var.container_name}
        auth: ${var.allow_shared_key ? "key" : "aad"}
      speech:
        endpoint: https://${azurerm_cognitive_account.speech.custom_subdomain_name}.cognitiveservices.azure.com
        auth: ${var.speech_local_auth ? "key" : "aad"}
  EOT
}
