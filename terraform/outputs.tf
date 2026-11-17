output "ssh" {
  description = "How Ansible (and you) reach the host."
  value       = "ssh -i secrets/vm_ed25519 -p ${var.ssh_port} ansible@127.0.0.1"
}

output "mariadb" {
  description = "MariaDB on the host, from this machine."
  value       = "127.0.0.1:${var.mariadb_port}"
}

output "container" {
  value = docker_container.host.name
}
