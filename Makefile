# Day-to-day commands. `make` on its own lists them.
SHELL := /bin/bash
-include .env
export

COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help up down ps logs replication accounts archivelog backup backups install health workload slow access test test-live oracle mariadb mariadb-replica mongo psql reset

help: ## List the commands
	@grep -hE '^[a-z-]+:.*## ' $(firstword $(MAKEFILE_LIST)) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'

up: .env secrets/mongo-keyfile ## Start every engine, wait until each is healthy, initiate Mongo, create the accounts
	mkdir -p backups/oracle backups/mariadb backups/mongo backups/postgres
	$(COMPOSE) up -d --wait --wait-timeout 600
	scripts/create-accounts.sh
	scripts/oracle/enable-archivelog.sh > /dev/null

down: ## Stop the lab, keep the data
	$(COMPOSE) down

ps: ## Show containers and their health
	$(COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'

logs: ## Follow logs; pick one with: make logs s=oracle
	$(COMPOSE) logs -f --tail 100 $(s)

replication: ## Seed the MariaDB replica from the primary and start GTID replication
	scripts/mariadb/setup-replication.sh

accounts: ## Create or update the approved accounts (dbops_monitor, shop_app) and rotate their passwords
	scripts/create-accounts.sh

# The replica set's shared secret: members prove to each other they belong. Never committed.
secrets/mongo-keyfile:
	mkdir -p secrets
	umask 077 && openssl rand -base64 756 > $@

archivelog: ## Put Oracle in ARCHIVELOG mode and set RMAN defaults (make up does this)
	scripts/oracle/enable-archivelog.sh

ENGINES_TO_BACK_UP := $(or $(e),postgres mariadb mongo oracle)
backup: ## Back up and restore-test every engine; one engine: make backup e=oracle
	@failed=""; for engine in $(ENGINES_TO_BACK_UP); do \
	  echo "=== $$engine"; scripts/backup/$$engine.sh || failed="$$failed $$engine"; echo; \
	done; \
	if [[ -n "$$failed" ]]; then echo "FAILED:$$failed"; exit 1; else echo "all backups verified"; fi

backups: ## List backups and their restore test results
	.venv/bin/dbops backups

install: ## Create .venv and install the dbops CLI with its test tools
	python3 -m venv .venv
	.venv/bin/pip install -q -e ".[dev]"

health: ## Health of every instance (exit 0 OK, 1 WARN, 2 CRIT, 3 UNKNOWN)
	.venv/bin/dbops health

workload: ## Run the application query mix for 30 s and report latency; make workload s=60
	.venv/bin/dbops workload --seconds $(or $(s),30)

slow: ## Slowest statements per engine with index suggestions; make slow a=--ddl, a=--reset
	.venv/bin/dbops slow $(a)

access: ## Access review against policy/access-policy.toml; make access a=--markdown for the evidence report
	.venv/bin/dbops access $(a)

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

mongo: ## mongosh on the rs0 primary, shop database, as dbops_admin
	docker exec -it dbops-mongo1 mongosh "mongodb://dbops_admin:$(MONGO_ADMIN_PASSWORD)@mongo1,mongo2,mongo3/shop?replicaSet=rs0&authSource=admin"

psql: ## psql into shop
	docker exec -it dbops-postgres psql -U postgres -d shop

reset: ## Delete every volume and start again from empty (asks first)
	@read -p "Delete all lab data? [y/N] " a && [[ $$a == y ]]
	$(COMPOSE) down -v

.env:
	@echo "No .env yet: run  cp .env.example .env  and change the passwords" && exit 1
