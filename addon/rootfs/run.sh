#!/usr/bin/with-contenv bashio

# ─────────────────────────────────────────────────────────────
# Helper: read a value from /data/options.json with jq
# Usage: _opt_value "mqtt.mqtt_host"  (supports dot-path)
# ─────────────────────────────────────────────────────────────
OPTIONS_FILE="/data/options.json"

_opt_value() {
    local key="$1"
    if [ -f "${OPTIONS_FILE}" ]; then
        # Convert dot-path to jq path: "mqtt.mqtt_host" -> .mqtt.mqtt_host
        local jq_path
        jq_path=$(echo "${key}" | sed 's/\./\./g' | sed 's/^/./')
        jq -r "${jq_path} // empty" "${OPTIONS_FILE}" 2>/dev/null || true
    fi
}

# ─────────────────────────────────────────────────────────────
# Get configuration from config.yaml options
# ─────────────────────────────────────────────────────────────
SERIAL_PORT=$(bashio::config 'serial_port')
TCP_PORT=$(bashio::config 'tcp_port')
LOG_LEVEL=$(bashio::config 'log_level')
CACHE_DEVICE_STATES=$(bashio::config 'cache_device_states')
MQTT_DISCOVERY_PREFIX=$(bashio::config 'mqtt.discovery_prefix')
MQTT_PREFIX=$(bashio::config 'mqtt.prefix')
MQTT_CLIENT_ID=$(bashio::config 'mqtt.client_id')

# ─────────────────────────────────────────────────────────────
# MQTT credentials — priority order:
#   1. /data/options.json mqtt.* (saved via the UI)
#   2. config.yaml mqtt.* options (set in HA addon config panel)
#   3. bashio::services mqtt (Supervisor auto-discovery — requires
#      the Mosquitto addon + "services: mqtt:need" to work)
# ─────────────────────────────────────────────────────────────

# Read from options.json first (saved via the web UI)
MQTT_HOST_OPT=$(_opt_value "mqtt.mqtt_host")
MQTT_PORT_OPT=$(_opt_value "mqtt.mqtt_port")
MQTT_USER_OPT=$(_opt_value "mqtt.mqtt_user")
MQTT_PWD_OPT=$(_opt_value "mqtt.mqtt_pwd")

if bashio::var.has_value "${MQTT_HOST_OPT}"; then
    # ── Source 1: UI / options.json ──
    MQTT_HOST="${MQTT_HOST_OPT}"
    MQTT_PORT="${MQTT_PORT_OPT:-1883}"
    MQTT_USER="${MQTT_USER_OPT}"
    MQTT_PASSWORD="${MQTT_PWD_OPT}"
    bashio::log.info "MQTT credentials loaded from options.json (UI config)"
else
    # ── Source 2: config.yaml options ──
    MQTT_HOST_CFG=$(bashio::config 'mqtt.mqtt_host' 2>/dev/null || true)
    MQTT_PORT_CFG=$(bashio::config 'mqtt.mqtt_port' 2>/dev/null || true)
    MQTT_USER_CFG=$(bashio::config 'mqtt.mqtt_user' 2>/dev/null || true)
    MQTT_PWD_CFG=$(bashio::config 'mqtt.mqtt_pwd' 2>/dev/null || true)

    if bashio::var.has_value "${MQTT_HOST_CFG}"; then
        MQTT_HOST="${MQTT_HOST_CFG}"
        MQTT_PORT="${MQTT_PORT_CFG:-1883}"
        MQTT_USER="${MQTT_USER_CFG}"
        MQTT_PASSWORD="${MQTT_PWD_CFG}"
        bashio::log.info "MQTT credentials loaded from config.yaml options"
    else
        # ── Source 3: Supervisor service discovery (Mosquitto addon) ──
        bashio::log.info "Trying MQTT service discovery via Supervisor..."
        MQTT_HOST=$(bashio::services mqtt "host" 2>/dev/null || true)
        MQTT_PORT=$(bashio::services mqtt "port" 2>/dev/null || true)
        MQTT_USER=$(bashio::services mqtt "username" 2>/dev/null || true)
        MQTT_PASSWORD=$(bashio::services mqtt "password" 2>/dev/null || true)

        if bashio::var.has_value "${MQTT_HOST}"; then
            bashio::log.info "MQTT credentials from Supervisor service discovery"
        else
            bashio::log.warning "No MQTT configuration found — running in UI-only mode"
            MQTT_HOST=""
            MQTT_PORT="1883"
            MQTT_USER=""
            MQTT_PASSWORD=""
        fi
    fi
fi

# ─────────────────────────────────────────────────────────────
# EnOcean connection: TCP takes priority over serial
# ─────────────────────────────────────────────────────────────
if bashio::var.has_value "${TCP_PORT}"; then
    ENOCEAN_PORT="${TCP_PORT}"
    bashio::log.info "Using TCP EnOcean connection: ${ENOCEAN_PORT}"
elif bashio::var.has_value "${SERIAL_PORT}"; then
    ENOCEAN_PORT="${SERIAL_PORT}"
    bashio::log.info "Using Serial EnOcean connection: ${ENOCEAN_PORT}"
else
    bashio::log.warning "No EnOcean port configured - running in UI-only mode"
    ENOCEAN_PORT=""
fi

# ─────────────────────────────────────────────────────────────
# Data path
# ─────────────────────────────────────────────────────────────
CONFIG_PATH="/data"

# One-time migration from old config path (/config/enocean → /data/)
if [ -d "/config/enocean" ] && [ ! -f "/config/.enocean_migrated" ]; then
    bashio::log.info "Migrating configuration from /config/enocean to ${CONFIG_PATH}/"
    cp -a /config/enocean/* "${CONFIG_PATH}/" 2>/dev/null || true
    touch /config/.enocean_migrated
    bashio::log.info "Migration complete"
fi

# ─────────────────────────────────────────────────────────────
# Export environment variables for the Python app
# ─────────────────────────────────────────────────────────────
export ENOCEAN_PORT="${ENOCEAN_PORT}"
export LOG_LEVEL="${LOG_LEVEL}"
export CACHE_DEVICE_STATES="${CACHE_DEVICE_STATES}"
export MQTT_HOST="${MQTT_HOST}"
export MQTT_PORT="${MQTT_PORT:-1883}"
export MQTT_USER="${MQTT_USER}"
export MQTT_PASSWORD="${MQTT_PASSWORD}"
export MQTT_DISCOVERY_PREFIX="${MQTT_DISCOVERY_PREFIX}"
export MQTT_PREFIX="${MQTT_PREFIX}"
export MQTT_CLIENT_ID="${MQTT_CLIENT_ID}"
export CONFIG_PATH="${CONFIG_PATH}"

# ─────────────────────────────────────────────────────────────
# Create data directories if they don't exist
# ─────────────────────────────────────────────────────────────
mkdir -p "${CONFIG_PATH}"
mkdir -p "${CONFIG_PATH}/custom_eep"

# ─────────────────────────────────────────────────────────────
# Log startup info
# ─────────────────────────────────────────────────────────────
bashio::log.info "Starting EnOcean MQTT..."
bashio::log.info "EnOcean Port: ${ENOCEAN_PORT:-not configured}"
bashio::log.info "Log Level: ${LOG_LEVEL}"
bashio::log.info "MQTT Broker: ${MQTT_HOST:-not configured}:${MQTT_PORT}"
bashio::log.info "MQTT Prefix: ${MQTT_PREFIX}"
bashio::log.info "Config Path: ${CONFIG_PATH}"

# ─────────────────────────────────────────────────────────────
# Start the application
# ─────────────────────────────────────────────────────────────
cd /app
exec python3 main.py
