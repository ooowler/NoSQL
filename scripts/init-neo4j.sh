#!/usr/bin/env bash
set -euo pipefail

host="${NEO4J_HOST:-neo4j}"
port="${NEO4J_PORT:-7687}"
username="${NEO4J_USERNAME:-neo4j}"
password="${NEO4J_PASSWORD:-password}"
uri="bolt://${host}:${port}"

until cypher-shell -a "$uri" -u "$username" -p "$password" "RETURN 1" >/dev/null 2>&1; do
    sleep 2
done

cypher-shell -a "$uri" -u "$username" -p "$password" <<CYPHER
CREATE CONSTRAINT user_id IF NOT EXISTS FOR (u:User) REQUIRE u.id IS UNIQUE;
CREATE CONSTRAINT event_id IF NOT EXISTS FOR (e:Event) REQUIRE e.id IS UNIQUE;
CYPHER
