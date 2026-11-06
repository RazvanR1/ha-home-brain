#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

LOG_LEVEL="$(bashio::config 'log_level')"
bashio::log.info "Home Brain porneste (log_level=${LOG_LEVEL})"

# Codul aplicatiei nu e scris inca. Pana atunci add-on-ul doar ramane pornit.
exec sleep infinity
