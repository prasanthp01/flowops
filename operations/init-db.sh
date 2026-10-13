#!/bin/sh
set -eu
flowops_owner_password=$(cat /run/secrets/owner_password)
flowops_runtime_password=$(cat /run/secrets/runtime_password)
psql --username postgres --dbname postgres --set ON_ERROR_STOP=1 <<EOSQL
CREATE ROLE flowops_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$flowops_owner_password';
CREATE ROLE flowops_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$flowops_runtime_password';
CREATE DATABASE flowops OWNER flowops_owner;
CREATE DATABASE flowops_test OWNER flowops_owner;
REVOKE ALL ON DATABASE flowops FROM PUBLIC;
GRANT CONNECT ON DATABASE flowops TO flowops_runtime;
REVOKE ALL ON DATABASE flowops_test FROM PUBLIC;
EOSQL
