#!/usr/bin/env bash
set -euo pipefail

first_cassandra_host() {
    local hosts="${CASSANDRA_HOSTS:-cassandra-test}"
    local host="${hosts%%,*}"
    echo "${host//[[:space:]]/}"
}

cqlsh_args() {
    local username="${CASSANDRA_USERNAME:-}"
    local password="${CASSANDRA_PASSWORD:-}"
    local args=()
    if [[ -n "$username" || -n "$password" ]]; then
        args=("-u" "$username" "-p" "$password")
    fi
    printf '%s\n' "${args[@]}"
}

run_cql() {
    local host="$1"
    local port="$2"
    mapfile -t auth_args < <(cqlsh_args)
    cqlsh "$host" "$port" "${auth_args[@]}" "$@"
}

host="$(first_cassandra_host)"
port="${CASSANDRA_PORT:-9042}"
keyspace="${CASSANDRA_KEYSPACE:-testkeyspace}"

if [[ ! "$keyspace" =~ ^[a-zA-Z][a-zA-Z0-9_]*$ ]]; then
    echo "invalid Cassandra keyspace: $keyspace" >&2
    exit 1
fi

until run_cql "$host" "$port" -e "DESCRIBE KEYSPACES" >/dev/null 2>&1; do
    sleep 2
done

run_cql "$host" "$port" <<CQL
CREATE KEYSPACE IF NOT EXISTS ${keyspace}
WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};

USE ${keyspace};

CREATE TABLE IF NOT EXISTS event_reactions (
    event_id text,
    like_value tinyint,
    created_by text,
    created_at timestamp,
    PRIMARY KEY ((event_id), created_by)
);

CREATE INDEX IF NOT EXISTS event_reactions_like_value_idx ON event_reactions (like_value);
CREATE INDEX IF NOT EXISTS event_reactions_created_by_idx ON event_reactions (created_by);
CQL
