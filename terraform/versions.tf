terraform {
  required_version = ">= 1.6"

  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.6"
    }
  }
}

# Docker Desktop's socket; DOCKER_HOST overrides it.
provider "docker" {
  host = var.docker_host
}
