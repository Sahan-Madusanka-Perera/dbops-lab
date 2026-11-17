# Day-to-day commands. `make` on its own lists them.
SHELL := /bin/bash
-include .env
export

COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help up down ps logs replication monitor-users install health test test-live oracle mariadb mariadb-replica mongo psql reset

help: ## List the commands
	@grep -hE '^[a-z-]+:.*## ' $(firstword $(MAKEFILE_LIST)) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'

up: .env ## Start every engine, wait until each is healthy, initiate the Mongo replica set
	$(COMPOSE) up -d --wait --wait-timeout 600
	$(COMPOSE) run --rm mongo-init
	scripts/create-monitor-users.sh

down: ## Stop the lab, keep the data
	$(COMPOSE) down

ps: ## Show containers and their health
	$(COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'

logs: ## Follow logs; pick one with: make logs s=oracle
	$(COMPOSE) logs -f --tail 100 $(s)

replication: ## Seed the MariaDB replica from the primary and start GTID replication
	scripts/mariadb/setup-replication.sh

monitor-users: ## Create or update the read-only dbops_monitor account in each engine
	scripts/create-monitor-users.sh

install: ## Create .venv and install the dbops CLI with its test tools
	python3 -m venv .venv
	.venv/bin/pip install -q -e ".[dev]"

health: ## Health of every instance (exit 0 OK, 1 WARN, 2 CRIT, 3 UNKNOWN)
	.venv/bin/dbops health

test: ## Unit tests, no databases needed
	.venv/bin/pytest -q

test-live: ## Tests against the running lab
	DBOPS_LIVE=1 .venv/bin/pytest -q tests/test_live.py

oracle: ## SQL*Plus into FREEPDB1 as SHOP
	docker exec -it dbops-oracle sqlplus -L "shop/$(ORACLE_APP_PASSWORD)@FREEPDB1"

mariadb: ## MariaDB client on the primary, shop database
	docker exec -it -e MYSQL_PWD="$(MARIADB_APP_PASSWORD)" dbops-mariadb-primary mariadb -ushop shop

mariadb-replica: ## MariaDB client on the replica, as root
	docker exec -it -e MYSQL_PWD="$(MARIADB_ROOT_PASSWORD)" dbops-mariadb-replica mariadb -uroot shop

mongo: ## mongosh on the rs0 primary, shop database
	docker exec -it dbops-mongo1 mongosh "mongodb://mongo1,mongo2,mongo3/shop?replicaSet=rs0"

psql: ## psql into shop
	docker exec -it dbops-postgres psql -U postgres -d shop

reset: ## Delete every volume and start again from empty (asks first)
	@read -p "Delete all lab data? [y/N] " a && [[ $$a == y ]]
	$(COMPOSE) down -v

.env:
	@echo "No .env yet: run  cp .env.example .env  and change the passwords" && exit 1
