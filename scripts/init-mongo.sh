#!/usr/bin/env bash
set -euo pipefail

wait_mongo() {
    local host="$1"
    local port="$2"
    until mongosh --quiet --host "$host" --port "$port" --eval 'db.adminCommand({ping: 1})' >/dev/null; do
        sleep 1
    done
}

wait_primary() {
    local host="$1"
    local port="$2"
    until mongosh --quiet --host "$host" --port "$port" --eval 'quit(db.hello().isWritablePrimary ? 0 : 1)'; do
        sleep 1
    done
}

wait_mongo mongo-config "$MONGODB_CONFIG_PORT"
wait_mongo mongo-shard01-primary "$MONGODB_SHARD01_PRIMARY_PORT"
wait_mongo mongo-shard02-primary "$MONGODB_SHARD02_PRIMARY_PORT"

mongosh --quiet --host mongo-config --port "$MONGODB_CONFIG_PORT" --eval "
try {
    rs.status()
} catch (e) {
    rs.initiate({
        _id: 'cfg',
        configsvr: true,
        members: [
            {_id: 0, host: 'mongo-config:${MONGODB_CONFIG_PORT}'}
        ]
    })
}
"

mongosh --quiet --host mongo-shard01-primary --port "$MONGODB_SHARD01_PRIMARY_PORT" --eval "
try {
    rs.status()
} catch (e) {
    rs.initiate({
        _id: 'shard01',
        members: [
            {_id: 0, host: 'mongo-shard01-primary:${MONGODB_SHARD01_PRIMARY_PORT}'},
            {_id: 1, host: 'mongo-shard01-secondary1:${MONGODB_SHARD01_SECONDARY1_PORT}'},
            {_id: 2, host: 'mongo-shard01-secondary2:${MONGODB_SHARD01_SECONDARY2_PORT}'}
        ]
    })
}
"

mongosh --quiet --host mongo-shard02-primary --port "$MONGODB_SHARD02_PRIMARY_PORT" --eval "
try {
    rs.status()
} catch (e) {
    rs.initiate({
        _id: 'shard02',
        members: [
            {_id: 0, host: 'mongo-shard02-primary:${MONGODB_SHARD02_PRIMARY_PORT}'},
            {_id: 1, host: 'mongo-shard02-secondary1:${MONGODB_SHARD02_SECONDARY1_PORT}'},
            {_id: 2, host: 'mongo-shard02-secondary2:${MONGODB_SHARD02_SECONDARY2_PORT}'}
        ]
    })
}
"

wait_primary mongo-config "$MONGODB_CONFIG_PORT"
wait_primary mongo-shard01-primary "$MONGODB_SHARD01_PRIMARY_PORT"
wait_primary mongo-shard02-primary "$MONGODB_SHARD02_PRIMARY_PORT"
wait_mongo mongos "$MONGODB_PORT"

mongosh --quiet --host mongos --port "$MONGODB_PORT" --eval "
const admin = db.getSiblingDB('admin')
const config = db.getSiblingDB('config')
const appdb = db.getSiblingDB('${MONGODB_DATABASE}')

if (config.shards.countDocuments({_id: 'shard01'}) === 0) {
    sh.addShard('shard01/mongo-shard01-primary:${MONGODB_SHARD01_PRIMARY_PORT},mongo-shard01-secondary1:${MONGODB_SHARD01_SECONDARY1_PORT},mongo-shard01-secondary2:${MONGODB_SHARD01_SECONDARY2_PORT}')
}

if (config.shards.countDocuments({_id: 'shard02'}) === 0) {
    sh.addShard('shard02/mongo-shard02-primary:${MONGODB_SHARD02_PRIMARY_PORT},mongo-shard02-secondary1:${MONGODB_SHARD02_SECONDARY1_PORT},mongo-shard02-secondary2:${MONGODB_SHARD02_SECONDARY2_PORT}')
}

admin.runCommand({enableSharding: '${MONGODB_DATABASE}'})
appdb.users.createIndex({username: 1}, {unique: true})

const titleIndex = appdb.events.getIndexes().find((index) => index.name === 'title_1')
if (titleIndex && titleIndex.unique) {
    appdb.events.dropIndex('title_1')
}

appdb.events.createIndex({title: 1})
appdb.events.createIndex({title: 1, created_by: 1})
appdb.events.createIndex({category: 1})
appdb.events.createIndex({'location.city': 1})

if (config.collections.countDocuments({_id: '${MONGODB_DATABASE}.events'}) === 0) {
    sh.shardCollection('${MONGODB_DATABASE}.events', {created_by: 'hashed'})
}
"
