variable "docker_host" {
  description = "Docker daemon to create the host on."
  type        = string
  default     = null
}

variable "name" {
  description = "Host name, also the container name prefix."
  type        = string
  default     = "db-vm1"
}

variable "ssh_public_key_path" {
  description = "Public key put in the ansible user's authorized_keys (make vm-up creates it)."
  type        = string
  default     = "../secrets/vm_ed25519.pub"
}

variable "ssh_port" {
  description = "Host port for SSH, on 127.0.0.1 only."
  type        = number
  default     = 2222
}

variable "mariadb_port" {
  description = "Host port for MariaDB, on 127.0.0.1 only."
  type        = number
  default     = 3318
}

variable "memory_mb" {
  description = "Memory limit for the host."
  type        = number
  default     = 1024
}
