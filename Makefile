.PHONY: run
run:
	docker compose --env-file .env.local up -d --build

.PHONY: stop
stop:
	docker compose --env-file .env.local down
