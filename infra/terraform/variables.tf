variable "subscription_id" {
  type        = string
  description = "Azure subscription to deploy into."
}

variable "location" {
  type        = string
  default     = "eastus2"
  description = "Region for the resource group and storage. Pick one close to you for upload speed."
}

variable "resource_group_name" {
  type    = string
  default = "rg-meetcap"
}

variable "storage_account_name" {
  type        = string
  description = "Globally unique, 3-24 lowercase letters and digits."
}

variable "container_name" {
  type    = string
  default = "meetings"
}

variable "allow_shared_key" {
  type        = bool
  default     = false
  description = "Keep false to force Entra ID (az login). Set true only if you must use a connection string."
}

variable "allowed_ip_ranges" {
  type        = list(string)
  default     = []
  description = "Optional public IPs/CIDRs allowed to reach the storage account (office egress, home). Empty = no IP filter."
}

variable "cool_after_days" {
  type    = number
  default = 30
}

variable "cold_after_days" {
  type    = number
  default = 90
}

variable "delete_after_days" {
  type        = number
  default     = 365
  description = "Retention. Align with your contracts and data policy."
}

variable "speech_name" {
  type        = string
  description = "Speech resource name; also used as its custom subdomain."
}

variable "speech_location" {
  type        = string
  default     = ""
  description = "Region for Speech. Must support fast transcription; empty = resource group region."
}

variable "speech_local_auth" {
  type        = bool
  default     = true
  description = "Allow key auth for Speech. Set false once az login (aad) works for you."
}

variable "create_openai" {
  type        = bool
  default     = false
  description = "Create an Azure OpenAI resource + deployment. Leave false to reuse an existing one."
}

variable "openai_name" {
  type    = string
  default = ""
}

variable "openai_location" {
  type    = string
  default = ""
}

variable "openai_deployment_name" {
  type    = string
  default = "gpt-4.1"
}

variable "openai_model" {
  type    = string
  default = "gpt-4.1"
}

variable "openai_model_version" {
  type    = string
  default = "2025-04-14"
}

variable "openai_capacity_k_tpm" {
  type    = number
  default = 50
}

variable "assign_roles" {
  type        = bool
  default     = true
  description = "Grant data-plane roles to user_object_id (or the caller). Needs Owner or User Access Administrator."
}

variable "user_object_id" {
  type        = string
  default     = null
  description = "Entra object id that will run meetcap. Defaults to whoever runs terraform."
}

variable "owner" {
  type    = string
  default = ""
}

variable "cost_center" {
  type    = string
  default = "solutions-engineering"
}

variable "tags" {
  type    = map(string)
  default = {}
}
