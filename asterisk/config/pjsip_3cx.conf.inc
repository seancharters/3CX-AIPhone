[threecx-reg]
type=registration
transport=transport-udp
outbound_auth=threecx-auth
server_uri=sip:${THREECX_HOST}:${THREECX_PORT}
client_uri=sip:${THREECX_EXTENSION}@${THREECX_HOST}
contact_user=${THREECX_EXTENSION}
; Short, so a registration left behind by a crash expires quickly (3CX would otherwise ring it too).
expiration=120
retry_interval=30
forbidden_retry_interval=300
max_retries=10000
; Match 3CX's incoming INVITEs to this registration even if they come from another IP.
line=yes
endpoint=threecx

[threecx-auth]
type=auth
auth_type=userpass
username=${THREECX_AUTH_ID}
password=${THREECX_PASSWORD}

[threecx]
type=aor
contact=sip:${THREECX_HOST}:${THREECX_PORT}
qualify_frequency=60

[threecx]
type=endpoint
transport=transport-udp
context=from-3cx
disallow=all
allow=g722,alaw,ulaw
outbound_auth=threecx-auth
aors=threecx
from_user=${THREECX_EXTENSION}
from_domain=${THREECX_HOST}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733

[threecx]
type=identify
endpoint=threecx
match=${THREECX_HOST}

