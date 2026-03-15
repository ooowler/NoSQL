.DEFAULT_GOAL = run

.PHONY: run
run:
	docker compose --env-file .env.local up -d --build

.PHONY: rund
rund:
	docker compose --env-file .env.local up --build

.PHONY: services
services:
	docker compose ps

.PHONY: stop
stop:
	docker compose --env-file .env.local down

.PHONY: clean
clean:
	docker compose --env-file .env.local down -v
