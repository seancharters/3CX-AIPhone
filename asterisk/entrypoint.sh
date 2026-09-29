#!/bin/sh
# Renders Asterisk config from the settings saved in the admin UI (/data/asterisk.env),
# re-renders and reloads whenever they change, and runs Asterisk in the foreground.
set -eu

SETTINGS=/data/asterisk.env
TEMPLATES=/etc/asterisk/templates
VARS='${THREECX_HOST} ${THREECX_PORT} ${THREECX_EXTENSION} ${THREECX_AUTH_ID} ${THREECX_PASSWORD} ${PUBLIC_IP} ${AMI_SECRET} ${TEST_SIP_PASSWORD}'

render() {
  # Defaults, then whatever the admin UI saved.
  THREECX_HOST= THREECX_PORT=5060 THREECX_EXTENSION= THREECX_AUTH_ID= THREECX_PASSWORD= PUBLIC_IP= TEST_SIP_PASSWORD=
  [ -f "$SETTINGS" ] && . "$SETTINGS"
  AMI_SECRET=$(cat /data/ami_secret)
  export THREECX_HOST THREECX_PORT THREECX_EXTENSION THREECX_AUTH_ID THREECX_PASSWORD PUBLIC_IP TEST_SIP_PASSWORD AMI_SECRET

  # Only substitute our own variables; leave Asterisk's ${EXTEN}, ${CHANNEL} etc. alone.
  for tmpl in "$TEMPLATES"/*.conf; do
    envsubst "$VARS" < "$tmpl" > "/etc/asterisk/$(basename "$tmpl")"
  done
  include() {  # include() <file> <condition>: render an optional section, or leave it empty
    if [ -n "$2" ]; then envsubst "$VARS" < "$TEMPLATES/$1" > "/etc/asterisk/$1"; else : > "/etc/asterisk/$1"; fi
  }
  include pjsip_nat.conf.inc "$PUBLIC_IP"
  include pjsip_3cx.conf.inc "$([ -n "$THREECX_HOST" ] && [ -n "$THREECX_EXTENSION" ] && [ -n "$THREECX_AUTH_ID" ] && echo yes)"
  include pjsip_tester.conf.inc "$TEST_SIP_PASSWORD"
  chown -R asterisk:asterisk /etc/asterisk
  chmod 640 /etc/asterisk/*.conf /etc/asterisk/*.inc
}

stamp() { stat -c '%Y %i' "$SETTINGS" 2>/dev/null || echo none; }  # inode changes on every save

# The agent creates the AMI secret on first start.
while [ ! -s /data/ami_secret ]; do echo "Waiting for /data/ami_secret..."; sleep 1; done

render
[ -n "${THREECX_HOST:-}" ] || echo "3CX extension not configured yet: open the admin UI to set it up."

# Watch for settings changes from the admin UI.
(
  last=$(stamp)
  while sleep 3; do
    now=$(stamp)
    if [ "$now" != "$last" ]; then
      last=$now
      echo "Settings changed: re-rendering config and reloading Asterisk"
      render
      asterisk -rx "core reload" >/dev/null || true
    fi
  done
) &

# On stop, unregister from 3CX first. Otherwise 3CX keeps the old registration until it
# expires and rings it as well as the new one, starting duplicate sessions for each call.
shutdown() {
  echo "Stopping: unregistering from 3CX"
  asterisk -rx "pjsip send unregister threecx-reg" >/dev/null 2>&1 || true
  sleep 1
  asterisk -rx "core stop now" >/dev/null 2>&1 || true
}
trap shutdown TERM INT

asterisk -f -U asterisk -G asterisk -vvv &
wait $!  # returns early when a signal arrives
wait $! 2>/dev/null || true  # then wait for Asterisk to finish stopping
