# One Linux host for the M6 Ansible roles, created the way a VM would be: an image,
# a network, disks that outlive the machine, and only SSH and MariaDB exposed.
#
# It is a container booting systemd rather than a VM, so it costs about 300 MB
# instead of a multi-GB VM image. Ansible cannot tell the difference: it gets SSH,
# systemd, apt and a real kernel's /proc. Moving to a cloud VM means swapping the
# resources here for the provider's instance, network and volume resources; the
# Ansible side does not change.

resource "docker_image" "db_host" {
  name = "dbops-lab/db-host:24.04"

  build {
    context = "${path.module}/images/db-host"
  }

  triggers = {
    dockerfile = filesha1("${path.module}/images/db-host/Dockerfile")
  }
}

resource "docker_network" "vm" {
  name = "dbops-vm"
}

# Disks: the database and its backups survive the host being replaced.
resource "docker_volume" "mysql" {
  name = "${var.name}-mysql"
}

resource "docker_volume" "backups" {
  name = "${var.name}-backups"
}

resource "docker_container" "host" {
  name     = "dbops-${var.name}"
  hostname = var.name
  image    = docker_image.db_host.image_id
  memory   = var.memory_mb
  restart  = "unless-stopped"

  # Equal to memory: no swap. A database that swaps is slower than one that is
  # told it is out of memory. (Left unset, Docker picks 2x memory and plans drift.)
  memory_swap = var.memory_mb

  # systemd as PID 1 needs to manage cgroups. privileged covers that in Docker
  # Desktop's VM; on a shared Docker host, use a real VM instead.
  privileged    = true
  cgroupns_mode = "host"

  tmpfs = {
    "/run"      = "rw"
    "/run/lock" = "rw"
  }

  volumes {
    host_path      = "/sys/fs/cgroup"
    container_path = "/sys/fs/cgroup"
    read_only      = false
  }

  volumes {
    volume_name    = docker_volume.mysql.name
    container_path = "/var/lib/mysql"
  }

  volumes {
    volume_name    = docker_volume.backups.name
    container_path = "/var/backups/dbops"
  }

  networks_advanced {
    name = docker_network.vm.name
  }

  ports {
    internal = 22
    external = var.ssh_port
    ip       = "127.0.0.1"
  }

  ports {
    internal = 3306
    external = var.mariadb_port
    ip       = "127.0.0.1"
  }

  # sshd reads authorized_keys as the user: root-owned is fine if it is not writable by others.
  upload {
    file        = "/home/ansible/.ssh/authorized_keys"
    content     = file(var.ssh_public_key_path)
    permissions = "0644"
  }
}
